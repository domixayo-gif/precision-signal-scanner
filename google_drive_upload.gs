/**
 * Google Apps Script endpoint for private IQ Option EMA audit CSV uploads.
 *
 * SETUP BEFORE DEPLOYING:
 * 1. Create a private folder in your Google Drive and paste its ID below.
 * 2. Generate a long random token (at least 32 characters) and paste it below.
 * 3. Deploy as a Web app, execute as "Me", access "Anyone".
 * 4. Save the deployment URL and the same token as GitHub Actions secrets:
 *    GDRIVE_WEB_APP_URL and GDRIVE_UPLOAD_TOKEN.
 *
 * Never put your real token in the public GitHub repository.
 */
const DRIVE_FOLDER_ID = "PASTE_YOUR_GOOGLE_DRIVE_FOLDER_ID_HERE";
const UPLOAD_TOKEN = "PASTE_A_LONG_RANDOM_SECRET_HERE";
const ALLOWED_FILES = new Set(["ema_trade_history", "ema_scan_audit"]);

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      throw new Error("Missing request body");
    }
    const payload = JSON.parse(e.postData.contents);
    if (!UPLOAD_TOKEN || UPLOAD_TOKEN.indexOf("PASTE_") === 0 ||
        payload.token !== UPLOAD_TOKEN) {
      throw new Error("Unauthorized");
    }
    if (!DRIVE_FOLDER_ID || DRIVE_FOLDER_ID.indexOf("PASTE_") === 0) {
      throw new Error("Drive folder is not configured");
    }
    const runId = String(payload.run_id || "").replace(/[^0-9A-Za-z_-]/g, "");
    if (!runId || !Array.isArray(payload.files) || payload.files.length > 2) {
      throw new Error("Invalid run or file list");
    }

    const folder = DriveApp.getFolderById(DRIVE_FOLDER_ID);
    const saved = [];
    payload.files.forEach(function(item) {
      const match = String(item.name || "").match(/^(ema_trade_history|ema_scan_audit)_run_[0-9A-Za-z_-]+\.csv$/);
      if (!match || !ALLOWED_FILES.has(match[1]) || !item.content_base64) {
        throw new Error("Invalid audit file");
      }
      const bytes = Utilities.base64Decode(item.content_base64);
      if (bytes.length > 8 * 1024 * 1024) {
        throw new Error("Audit file exceeds 8 MB limit");
      }
      const content = Utilities.newBlob(bytes).getDataAsString("UTF-8");
      const iterator = folder.getFilesByName(item.name);
      let file;
      let action;
      if (iterator.hasNext()) {
        file = iterator.next();
        file.setContent(content);
        action = "updated";
      } else {
        file = folder.createFile(item.name, content, MimeType.CSV);
        action = "created";
      }
      saved.push({name: file.getName(), action: action});
    });
    return jsonOutput({ok: true, files: saved});
  } catch (err) {
    return jsonOutput({ok: false, error: String(err && err.message || err)});
  }
}

function jsonOutput(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
