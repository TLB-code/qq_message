# QQ Message Summary

A personal QQ group message summary tool backed by DeepSeek.

The first version is intentionally small: it receives OneBot/NapCat group message events, stores them in SQLite, tracks which messages have already been summarized, and lets you summarize new messages from a local web page.

## Features

- OneBot HTTP webhook ingestion at `/webhook/onebot`.
- SQLite message storage.
- Per-group unread cursor based on the last summarized message.
- DeepSeek chat-completions summarization.
- Local web UI for selecting groups, viewing unread messages, and reading summary history.

## Requirements

- Python 3.11 or newer.
- A DeepSeek API key.
- NapCatQQ or another OneBot-compatible QQ adapter.

This project uses only the Python standard library for the MVP, so there is no dependency installation step.

## Configuration

Copy `.env.example` to `.env` or set environment variables directly:

```powershell
$env:DEEPSEEK_API_KEY="sk-your-deepseek-api-key"
$env:DEEPSEEK_MODEL="deepseek-v4-flash"
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
```

DeepSeek's current OpenAI-compatible base URL is `https://api.deepseek.com`.

## Run

```powershell
python -m app.server
```

Open:

```text
http://127.0.0.1:8000
```

The SQLite database is created at:

```text
data/qq_summary.sqlite3
```

## NapCat / OneBot setup

Configure NapCat's HTTP POST event reporting URL to:

```text
http://127.0.0.1:8000/webhook/onebot
```

When group messages arrive, groups will appear in the web UI automatically.

## Manual test event

You can inject a fake group message with PowerShell:

```powershell
$body = @{
  post_type = "message"
  message_type = "group"
  time = [int][double]::Parse((Get-Date -UFormat %s))
  message_id = 1001
  group_id = 123456
  user_id = 42
  raw_message = "明天下午三点开会，记得带方案。"
  sender = @{
    nickname = "张三"
    card = "张三"
  }
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/webhook/onebot" -Body $body -ContentType "application/json"
```

## Notes

This is a personal-use local tool. Non-official QQ adapters can break after QQ client or protocol changes and may trigger account risk controls. Use a separate QQ account where possible, avoid automated sending, and keep private group messages local unless you intentionally send them to the AI API for summarization.
