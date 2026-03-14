"""Email notifications via the Resend API."""
from datetime import datetime, timezone

import resend

from app.core.config import get_settings

settings = get_settings()
resend.api_key = settings.resend_api_key

# ---------------------------------------------------------------------------
# HTML email template (JHBridge brand: #1A365D / #68D391)
# ---------------------------------------------------------------------------
_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body {{ margin:0; padding:20px; background:#F7FAFC;
         font-family:Inter,Roboto,sans-serif; }}
  .wrap {{ max-width:600px; margin:0 auto; }}
  .hd   {{ background:#1A365D; color:#fff; padding:32px 28px;
           border-radius:12px 12px 0 0; }}
  .hd h1{{ margin:0; font-size:22px; letter-spacing:.5px; }}
  .hd p {{ margin:6px 0 0; font-size:13px; opacity:.75; }}
  .bd   {{ background:#fff; padding:28px;
           border-radius:0 0 12px 12px;
           box-shadow:0 4px 16px rgba(0,0,0,.08); }}
  .badge{{ display:inline-block; padding:6px 18px; border-radius:20px;
           font-weight:700; font-size:13px; }}
  .ok   {{ background:#C6F6D5; color:#22543D; }}
  .fail {{ background:#FED7D7; color:#742A2A; }}
  table {{ width:100%; border-collapse:collapse; margin-top:16px; }}
  td    {{ padding:10px 0; border-bottom:1px solid #EDF2F7;
           font-size:13px; color:#4A5568; }}
  td:last-child{{ font-weight:600; color:#1A202C; text-align:right; }}
  .cta  {{ text-align:center; margin:24px 0 8px; }}
  .cta a{{ background:#68D391; color:#1A365D; padding:12px 28px;
           border-radius:8px; text-decoration:none;
           font-weight:700; font-size:14px; }}
  .err  {{ background:#FFF5F5; border-left:4px solid #FC8181;
           padding:14px; margin-top:16px; border-radius:4px;
           font-size:13px; color:#742A2A; }}
  .ft   {{ text-align:center; margin-top:20px; color:#A0AEC0;
           font-size:11px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hd">
    <h1>JHBridge &mdash; Backup Report</h1>
    <p>Automated Database Backup Notification</p>
  </div>
  <div class="bd">
    <p style="color:#4A5568;font-size:14px;">
      Your scheduled database backup has been processed:
    </p>
    <div style="text-align:center;margin:18px 0;">
      <span class="badge {badge_class}">{status_label}</span>
    </div>
    <table>
      <tr><td>Task ID</td>      <td>{task_id}</td></tr>
      <tr><td>Triggered By</td> <td>{triggered_by}</td></tr>
      <tr><td>Database</td>     <td>{db_masked}</td></tr>
      <tr><td>File Size</td>    <td>{file_size}</td></tr>
      <tr><td>Duration</td>     <td>{duration}</td></tr>
      <tr><td>Timestamp</td>    <td>{timestamp}</td></tr>
    </table>
    {cta_block}
    {err_block}
  </div>
  <div class="ft">
    JHBridge Translation Services &bull; Automated Backup System<br>
    This is an automated message &mdash; please do not reply.
  </div>
</div>
</body>
</html>
"""


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1_048_576:
        return f"{b/1024:.1f} KB"
    if b < 1_073_741_824:
        return f"{b/1_048_576:.1f} MB"
    return f"{b/1_073_741_824:.2f} GB"


def send_backup_report(
    to_email: str,
    task_id: str,
    status: str,
    triggered_by: str = "SYSTEM",
    db_masked: str = "***",
    s3_url: str = "",
    file_size_bytes: int = 0,
    duration_seconds: float = 0.0,
    error_message: str = "",
) -> bool:
    """Send an HTML backup report email via Resend. Returns True on success."""
    try:
        ok = status == "COMPLETED"
        badge_class = "ok" if ok else "fail"
        status_label = "COMPLETED" if ok else "FAILED"

        cta_block = ""
        if s3_url and ok:
            cta_block = (
                f'<div class="cta">'
                f'<a href="{s3_url}">Download Backup File</a>'
                f"</div>"
            )

        err_block = ""
        if error_message:
            err_block = (
                f'<div class="err"><strong>Error:</strong> {error_message}</div>'
            )

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        subject_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

        html = _TEMPLATE.format(
            badge_class=badge_class,
            status_label=status_label,
            task_id=f"{task_id[:8]}...",
            triggered_by=triggered_by,
            db_masked=db_masked,
            file_size=_fmt_size(file_size_bytes),
            duration=f"{duration_seconds:.1f}s",
            timestamp=now_utc,
            cta_block=cta_block,
            err_block=err_block,
        )

        resend.Emails.send({
            "from": settings.email_from,
            "to": [to_email],
            "subject": (
                f"[JHBridge Backup] {status_label} - {subject_date} UTC"
            ),
            "html": html,
        })
        return True

    except Exception as exc:
        print(f"[ses_service] Email send failed: {exc}")
        return False
