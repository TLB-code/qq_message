# Startup Guide

This guide describes how to start the local QQ group summary tool after a reboot.

## 1. Open PowerShell

```powershell
cd D:\tim\Desktop\qq_message
```

## 2. Check `.env`

Make sure `D:\tim\Desktop\qq_message\.env` exists and contains your DeepSeek key:

```text
DEEPSEEK_API_KEY=sk-your-real-key
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
QQ_SUMMARY_HOST=127.0.0.1
QQ_SUMMARY_PORT=8000
QQ_SUMMARY_WEBHOOK_DEBUG=false
```

Do not put your real key in `.env.example`.

## 3. Start The Web Service

```powershell
python -m app.server
```

Keep this PowerShell window open.

Open the summary page:

```text
http://127.0.0.1:8000
```

Health check:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health"
```

## 4. Start NapCat

Open another PowerShell window:

```powershell
cd D:\tim\Desktop\qq_message\tools\napcat\onekey\NapCat.44498.Shell
.\napcat.bat
```

Keep this window open too.

NapCat WebUI:

```text
http://127.0.0.1:6099/webui/?token=63d4a7435850
```

## 5. Confirm QQ Login

If NapCat shows a QR code, scan it with QQ and authorize login.

If it says the account is already logged in elsewhere, close the normal QQ client first, then retry NapCat login.

## 6. Confirm NapCat Webhook

NapCat should have an HTTP Client named:

```text
qq-message-summary
```

It should point to:

```text
http://127.0.0.1:8000/webhook/onebot
```

Expected settings:

```text
enable: true
messagePostFormat: array
reportSelfMessage: false
token: empty
```

## 7. Wait For Group Messages

This tool summarizes messages received after NapCat is connected. It does not automatically import old QQ unread messages from the QQ client.

When new group messages arrive, refresh:

```text
http://127.0.0.1:8000
```

Then select a group and click:

```text
总结未读
```

## Useful Checks

Check groups currently stored:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/groups" | ConvertTo-Json -Depth 10
```

Check whether the DeepSeek key is loaded without printing the key:

```powershell
python -c "from app.config import load_settings; s=load_settings(); print(bool(s.deepseek_api_key))"
```

Check NapCat WebUI port:

```powershell
Get-NetTCPConnection -LocalPort 6099 -ErrorAction SilentlyContinue
```

Check web service port:

```powershell
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
```

## Restart Only The Web Service

Find running web service processes:

```powershell
Get-CimInstance Win32_Process -Filter "name = 'python.exe'" |
  Where-Object { $_.CommandLine -like '*-m app.server*' } |
  Select-Object ProcessId,CommandLine
```

Stop them:

```powershell
Stop-Process -Id <PID> -Force
```

Start again:

```powershell
cd D:\tim\Desktop\qq_message
python -m app.server
```

## Restart Only NapCat

Close the NapCat command window, or stop only processes under:

```text
D:\tim\Desktop\qq_message\tools\napcat\onekey\NapCat.44498.Shell
```

Then start again:

```powershell
cd D:\tim\Desktop\qq_message\tools\napcat\onekey\NapCat.44498.Shell
.\napcat.bat
```

## Troubleshooting

### Page Has No New Groups

Check whether NapCat is logged in and whether new QQ group messages arrived after startup.

Then check:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/groups" | ConvertTo-Json -Depth 10
```

### DeepSeek Says API Key Is Missing

Confirm `.env` is in:

```text
D:\tim\Desktop\qq_message\.env
```

Then restart the web service.

### Summary Fails

Check that the model and base URL are:

```text
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

Then retry `总结未读`.

### Need Raw Webhook Debug Logs

Set this in `.env`:

```text
QQ_SUMMARY_WEBHOOK_DEBUG=true
```

Restart the web service. Raw webhook events will be written to:

```text
data\webhook_events.log
```

Turn it back off after debugging because this log contains message content.

