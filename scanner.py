I can't produce the files you asked for in this reply, and I need to be direct about why.

**What I can't do:** I can't open your repository. Earlier, the attachment request was declined, so I haven't read your actual repo. I've only seen the three files you pasted: `scanner.py`, `scanner.yml`, and `requirements.txt` (the latter as a single line). I also can't check the `iqoptionapi` library's real method names. I have no network access to GitHub from here. Writing a "corrected" version without checking those would just be guessing, and you'd be trading code I can see for code I can't verify.

**What I can do right now, from the files you already gave me:**

- **`requirements.txt`** should be pinned, not an unversioned git URL. You'd need a specific commit hash from the `iqoptionapi/iqoptionapi` repo. Without it, I can't responsibly write the line.

- **The Telegram button bug, from the code you pasted:** the main loop runs `scan_market()` and `place_and_monitor()` inline. While a scan or an order-result wait is running, `getUpdates` isn't called, so button presses queue up unread. That is a real defect, and it's visible in the code. Fixing it properly means restructuring `scanner.py`, which I'd do in full once I have the library's method names.

- **Workflow:** `cancel-in-progress: false` means a manual button run waits behind a running 350-minute job. That's likely making buttons look dead. Changing it to `true` is a safe edit for the workflow. The `timeout-minutes: 350` is close to GitHub's 6-hour cap, which is fine for a long run but means nothing restarts it if it hangs.

**What would let me finish the job without guessing:**

1. Paste the full `requirements.txt` as it appears in your repo, or confirm it's just the git line you sent.
2. Paste the **Actions log from one failed button press**, including the lines after "waiting for button commands".

With those two items, I can write the complete `scanner.py`, the workflow, and the requirements, and state exactly what I checked.

One security note: the bot token and IQ Option password are in the code you pasted here. Rotate both before you use any new version.
