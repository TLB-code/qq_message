from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from .storage import Store


MAX_VOICE_BYTES = 25 * 1024 * 1024


class VoiceArchiveError(RuntimeError):
    pass


class VoiceArchiveManager:
    def __init__(
        self,
        store: Store,
        media_root: Path,
        source_root: Path,
        ffmpeg_path: str = "ffmpeg",
        napcat_api_url: str | None = None,
        napcat_access_token: str | None = None,
        enabled: bool = True,
        max_voice_bytes: int = MAX_VOICE_BYTES,
    ) -> None:
        self.store = store
        self.media_root = media_root.expanduser()
        self.source_root = source_root.expanduser()
        self.ffmpeg_path = ffmpeg_path
        self.napcat_api_url = napcat_api_url.rstrip("/") if napcat_api_url else None
        self.napcat_access_token = napcat_access_token
        self.enabled = enabled
        self.max_voice_bytes = max(int(max_voice_bytes), 1)
        self._queue: queue.Queue[int] = queue.Queue()
        self._queued: set[int] = set()
        self._queued_lock = threading.Lock()
        self._worker_started = False

    def start(self) -> int:
        if not self.enabled:
            return 0
        self.media_root.mkdir(parents=True, exist_ok=True)
        if not self._worker_started:
            self._worker_started = True
            threading.Thread(
                target=self._worker,
                name="voice-archive-worker",
                daemon=True,
            ).start()
        recoverable = self.store.list_recoverable_message_media()
        for media in recoverable:
            self.enqueue(int(media["id"]))
        return len(recoverable)

    def enqueue(self, media_id: int, force: bool = False) -> bool:
        if not self.enabled:
            return False
        media_id = int(media_id)
        if force and not self.store.reset_message_media_for_retry(media_id, int(time.time())):
            return False
        with self._queued_lock:
            if media_id in self._queued:
                return False
            self._queued.add(media_id)
        self._queue.put(media_id)
        return True

    def resolve_playback_path(self, media: dict[str, Any]) -> Path | None:
        return self._resolve_stored_path(media.get("playback_path"))

    def delete_files(self, media_rows: list[dict[str, Any]]) -> int:
        deleted = 0
        for media in media_rows:
            for key in ("original_path", "playback_path"):
                path = self._resolve_stored_path(media.get(key))
                if path is None or not path.is_file():
                    continue
                try:
                    path.unlink()
                    deleted += 1
                    self._remove_empty_parents(path.parent)
                except OSError:
                    continue
        return deleted

    def _worker(self) -> None:
        while True:
            media_id = self._queue.get()
            try:
                self._process_with_retries(media_id)
            finally:
                with self._queued_lock:
                    self._queued.discard(media_id)
                self._queue.task_done()

    def _process_with_retries(self, media_id: int) -> None:
        media = self.store.get_message_media(media_id)
        if media is None or media.get("status") == "ready":
            return
        if not self.store.mark_message_media_processing(media_id, int(time.time())):
            return

        error: Exception | None = None
        for attempt, delay in enumerate((1, 3, 0), start=1):
            try:
                original_path, playback_path = self._archive_voice(media)
                relative_original = self._relative_path(original_path) if original_path else None
                relative_playback = self._relative_path(playback_path)
                completed = self.store.complete_message_media(
                    media_id,
                    original_path=relative_original,
                    playback_path=relative_playback,
                    mime_type="audio/mpeg",
                    size_bytes=playback_path.stat().st_size,
                    updated_at=int(time.time()),
                )
                if not completed:
                    self.delete_files(
                        [
                            {
                                "original_path": relative_original,
                                "playback_path": relative_playback,
                            }
                        ]
                    )
                    return
                print(
                    "Voice archive completed: "
                    f"media_id={media_id} message_id={media['message_id']} "
                    f"size_bytes={playback_path.stat().st_size}",
                    flush=True,
                )
                return
            except Exception as exc:  # noqa: BLE001 - worker must persist failures
                error = exc
                if delay:
                    print(
                        "Voice archive retry: "
                        f"media_id={media_id} attempt={attempt}/3 reason={type(exc).__name__}",
                        flush=True,
                    )
                    time.sleep(delay)

        error_text = str(error or "unknown voice archive error")
        self.store.fail_message_media(media_id, error_text, int(time.time()))
        print(
            "Voice archive failed: "
            f"media_id={media_id} message_id={media['message_id']} error={error_text}",
            flush=True,
        )

    def _archive_voice(self, media: dict[str, Any]) -> tuple[Path | None, Path]:
        media_id = int(media["id"])
        group_id = self._safe_component(str(media["group_id"]))
        day = datetime.fromtimestamp(int(media["created_at"])).strftime("%Y-%m-%d")
        source_name = self._source_basename(str(media.get("source_name") or ""))
        if not source_name:
            raise VoiceArchiveError("voice segment does not contain a file name")

        original_dir = self.media_root / "original" / group_id / day
        playback_dir = self.media_root / "playback" / group_id / day
        original_dir.mkdir(parents=True, exist_ok=True)
        playback_dir.mkdir(parents=True, exist_ok=True)
        source_suffix = Path(source_name).suffix.lower() or ".amr"
        original_path = original_dir / f"{media_id}{source_suffix}"
        playback_path = playback_dir / f"{media_id}.mp3"

        local_source = self._find_local_source(source_name, int(media["created_at"]))
        if local_source is not None:
            self._copy_file(local_source, original_path)

        if self.napcat_api_url:
            try:
                api_source = self._download_napcat_record(source_name, media_id)
                try:
                    if self._is_amr(api_source):
                        if not original_path.is_file():
                            self._copy_file(api_source, original_path)
                        self._transcode_to_mp3(original_path, playback_path)
                    else:
                        self._copy_file(api_source, playback_path)
                    return (original_path if original_path.is_file() else None), playback_path
                finally:
                    self._remove_api_temp(api_source)
            except VoiceArchiveError as exc:
                print(
                    "Voice archive NapCat fallback: "
                    f"media_id={media_id} reason={exc}",
                    flush=True,
                )

        if not original_path.is_file():
            raise VoiceArchiveError(
                f"voice file {source_name} was not found under {self.source_root}"
            )
        if original_path.suffix.lower() == ".mp3" and not self._is_amr(original_path):
            self._copy_file(original_path, playback_path)
        else:
            self._transcode_to_mp3(original_path, playback_path)
        return original_path, playback_path

    def _find_local_source(self, source_name: str, created_at: int) -> Path | None:
        root = self.source_root
        if not root.is_dir():
            return None
        month = datetime.fromtimestamp(created_at).strftime("%Y-%m")
        direct_candidates = [
            root / "nt_data" / "Ptt" / month / "Ori" / source_name,
        ]
        try:
            account_dirs = [item for item in root.iterdir() if item.is_dir()]
        except OSError:
            account_dirs = []
        direct_candidates.extend(
            account / "nt_data" / "Ptt" / month / "Ori" / source_name
            for account in account_dirs
        )
        for candidate in direct_candidates:
            if candidate.is_file():
                return candidate

        for current_root, _dirs, files in os.walk(root):
            if source_name in files:
                return Path(current_root) / source_name
        return None

    def _download_napcat_record(self, source_name: str, media_id: int) -> Path:
        endpoint = self.napcat_api_url or ""
        if not endpoint.endswith("/get_record"):
            endpoint = f"{endpoint}/get_record"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "QQSummary/0.1",
        }
        if self.napcat_access_token:
            headers["Authorization"] = f"Bearer {self.napcat_access_token}"
        request = Request(
            endpoint,
            data=json.dumps(
                {"file": source_name, "out_format": "mp3"},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read(self.max_voice_bytes * 2 + 1).decode("utf-8")
        except HTTPError as exc:
            raise VoiceArchiveError(f"NapCat get_record returned HTTP {exc.code}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise VoiceArchiveError(f"NapCat get_record request failed: {exc}") from exc

        try:
            payload = json.loads(body)
            if int(payload.get("retcode", 0)) != 0:
                raise VoiceArchiveError(
                    f"NapCat get_record failed: {payload.get('message') or payload.get('wording')}"
                )
            data = payload.get("data") or {}
            file_value = data.get("file") if isinstance(data, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VoiceArchiveError("NapCat get_record returned invalid JSON") from exc
        if not isinstance(file_value, str) or not file_value:
            raise VoiceArchiveError("NapCat get_record did not return a file")

        temp_dir = self.media_root / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / f"{media_id}-napcat"
        if file_value.startswith("base64://"):
            try:
                content = base64.b64decode(file_value.removeprefix("base64://"), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise VoiceArchiveError("NapCat returned invalid base64 audio") from exc
            self._write_bytes(content, temp_path)
            return temp_path

        parsed = urlparse(file_value)
        if parsed.scheme in {"http", "https"}:
            media_request = Request(
                file_value,
                headers={"User-Agent": "QQSummary/0.1"},
            )
            try:
                with urlopen(media_request, timeout=30) as response:
                    content = response.read(self.max_voice_bytes + 1)
            except (HTTPError, TimeoutError, URLError, OSError) as exc:
                raise VoiceArchiveError(f"unable to download NapCat audio: {exc}") from exc
            self._write_bytes(content, temp_path)
            return temp_path

        if parsed.scheme == "file":
            source_path = Path(unquote(parsed.path))
        else:
            source_path = Path(file_value)
        if not source_path.is_file():
            raise VoiceArchiveError("NapCat returned an unavailable local audio path")
        return source_path

    def _transcode_to_mp3(self, source: Path, destination: Path) -> None:
        if shutil.which(self.ffmpeg_path) is None and not Path(self.ffmpeg_path).is_file():
            raise VoiceArchiveError(f"ffmpeg executable was not found: {self.ffmpeg_path}")
        temp_path = destination.with_name(f"{destination.stem}.tmp.mp3")
        temp_path.unlink(missing_ok=True)
        try:
            result = subprocess.run(
                [
                    self.ffmpeg_path,
                    "-nostdin",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-vn",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "5",
                    str(temp_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceArchiveError(f"ffmpeg failed to start: {exc}") from exc
        if result.returncode != 0 or not temp_path.is_file() or temp_path.stat().st_size <= 0:
            temp_path.unlink(missing_ok=True)
            detail = (result.stderr or "unknown ffmpeg error").strip()[-300:]
            raise VoiceArchiveError(f"ffmpeg conversion failed: {detail}")
        os.replace(temp_path, destination)

    def _copy_file(self, source: Path, destination: Path) -> None:
        size = source.stat().st_size
        if size <= 0:
            raise VoiceArchiveError("voice file is empty")
        if size > self.max_voice_bytes:
            raise VoiceArchiveError("voice file is too large")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_name(f"{destination.name}.tmp")
        try:
            shutil.copyfile(source, temp_path)
            os.replace(temp_path, destination)
        finally:
            temp_path.unlink(missing_ok=True)

    def _write_bytes(self, content: bytes, destination: Path) -> None:
        if not content:
            raise VoiceArchiveError("voice file is empty")
        if len(content) > self.max_voice_bytes:
            raise VoiceArchiveError("voice file is too large")
        destination.write_bytes(content)

    def _is_amr(self, path: Path) -> bool:
        try:
            with path.open("rb") as file:
                return file.read(9).startswith(b"#!AMR")
        except OSError as exc:
            raise VoiceArchiveError(f"unable to inspect voice file: {exc}") from exc

    def _resolve_stored_path(self, value: Any) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        root = self.media_root.resolve()
        candidate = (root / value).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.media_root.resolve()).as_posix()

    def _remove_api_temp(self, path: Path) -> None:
        temp_root = (self.media_root / ".tmp").resolve()
        try:
            path.resolve().relative_to(temp_root)
        except ValueError:
            return
        path.unlink(missing_ok=True)

    def _remove_empty_parents(self, directory: Path) -> None:
        root = self.media_root.resolve()
        current = directory.resolve()
        while current != root:
            try:
                current.relative_to(root)
                current.rmdir()
            except (OSError, ValueError):
                return
            current = current.parent

    @staticmethod
    def _source_basename(value: str) -> str:
        return Path(value.replace("\\", "/")).name.strip()

    @staticmethod
    def _safe_component(value: str) -> str:
        cleaned = "".join(char for char in value if char.isalnum() or char in {"-", "_"})
        return cleaned or "unknown"
