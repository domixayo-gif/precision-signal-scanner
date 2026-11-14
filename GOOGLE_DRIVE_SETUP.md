# Google Drive storage for EMA audit CSVs

The scanner can upload its two private audit CSVs to a Google Drive folder at the
end of each GitHub Actions run. Each run gets its own pair of files, so later
runs do not overwrite earlier test data.

## 1. Create a Drive folder

Create a folder in your Google Drive, for example `IQ Option EMA Audit`.
Open the folder and copy the ID from its URL (the part after `/folders/`).

## 2. Create the Apps Script endpoint

1. Visit https://script.google.com and create a new project.
2. Replace the editor contents with the code in `google_drive_upload.gs`.
3. Replace `PASTE_YOUR_GOOGLE_DRIVE_FOLDER_ID_HERE` with the folder ID.
4. Replace `PASTE_A_LONG_RANDOM_SECRET_HERE` with a new random secret of at
   least 32 characters. Do not reuse your Google password.
5. Save the project.
6. Select **Deploy → New deployment → Web app**.
7. Set **Execute as: Me** and **Who has access: Anyone**, then deploy and
   authorize the script to create files in your Drive.
8. Copy the Web app URL ending in `/exec`.

The endpoint is publicly reachable because GitHub Actions needs to call it.
The shared random token is checked before any file is written. Keep that token
private and do not share the deployment URL publicly.

## 3. Add two GitHub Actions secrets

In the repository, open **Settings → Secrets and variables → Actions → New
repository secret**. Add:

- `GDRIVE_WEB_APP_URL`: the Apps Script Web app URL ending in `/exec`
- `GDRIVE_UPLOAD_TOKEN`: the exact same random secret entered in Apps Script

Never commit either value to the repository.

## 4. Verify the first upload

After both secrets are saved, the next scanner run will upload any audit CSVs
it produced. The files will appear in your Drive folder with the run ID in the
filename, for example `ema_trade_history_run_123456.csv`.

If you change the Apps Script code later, deploy a **new version** of the web
app so the existing /exec URL uses the updated code.

## Privacy and limits

- CSVs are not committed to this public repository.
- Uploads are made only from non-pull-request scanner runs.
- Each workflow run is stored as a separate snapshot in your Drive folder.
- Only the two named audit CSVs are accepted by the endpoint.
- The uploader does not upload IQ Option passwords, Telegram tokens, or other
  GitHub secrets.
