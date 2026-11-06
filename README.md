# X Unfollow Tool

Unfollow accounts from your X Following list using your existing signed-in Chrome session. The tool can wait and work while you use another tab or app. It reports how many unfollow controls it sees by default. Pass `--apply` to perform unfollows.

## Setup

Install Python 3.10 or newer and Google Chrome 144 or newer. Use X in English. Run the commands below from the repository directory.

**PowerShell 7+**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

**Linux/Bash**

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
```

**Both platforms**

```text
python -m pip install -r requirements.txt
```

Playwright connects to the Chrome instance you already use. In Chrome, visit `chrome://inspect/#remote-debugging` and enable remote debugging. Sign in to X and open your own Following page at `https://x.com/YOUR_HANDLE/following`. Keep exactly one copy of that tab open. Select **Allow** when Chrome asks to approve a connection. [Chrome DevTools documentation](https://developer.chrome.com/docs/devtools/agents/use-cases/auto-connect)

## Run

Replace `YOUR_HANDLE` with your X handle. These commands work in PowerShell 7+ and Linux/Bash.

Check that the tool can find unfollow controls in the Following tab.

```text
python unfollow.py YOUR_HANDLE
```

Unfollow up to five accounts in a trial run.

```text
python unfollow.py YOUR_HANDLE --apply --max-actions 5
```

To continue through the Following list, leave out `--max-actions`.

```text
python unfollow.py YOUR_HANDLE --apply
```

Keep Chrome, the Following tab, and the terminal open during a run. You can work in other tabs and apps. Press **Ctrl+C** in the terminal to stop. If a confirmation dialog is still open, dismiss it before restarting. Rerun the command to continue with the same pacing history. Chrome and its tabs stay open when the tool exits.

The tool stops on a rate-limit response, an account restriction message, or an unfollow it cannot verify. It relies on X's English interface and may need an update if X changes the Following page.

## Pacing

Edit `.env` to change the local pacing limits. Use positive integers for the limits and intervals. Set jitter to `0` to disable it.

| Setting | Default | What it controls |
| --- | ---: | --- |
| `UNFOLLOW_DAILY_LIMIT` | 400 | Maximum attempts in a rolling 24-hour window, estimated from X's daily following cap |
| `UNFOLLOW_HOURLY_LIMIT` | 30 | Maximum attempts in a rolling 60-minute window |
| `UNFOLLOW_MIN_INTERVAL_SECONDS` | 30 | Minimum time between attempts |
| `UNFOLLOW_INTERVAL_JITTER_SECONDS` | 15 | Maximum random delay added to the minimum interval |
| `UNFOLLOW_BATCH_SIZE` | 20 | Number of attempts before a batch cooldown |
| `UNFOLLOW_BATCH_COOLDOWN_SECONDS` | 3600 | Minimum time to wait after a batch |

With the defaults, the tool waits 30–45 seconds between attempts when budgets allow. It samples an extra delay uniformly from 0 to 15 seconds and saves it with each attempt so it survives a restart. A cap or batch cooldown can extend the wait.

The tool tracks its attempts per handle in `db.sqlite3` in the repository directory. It counts an attempt before acting so an interruption or unconfirmed result still consumes that slot. Actions taken elsewhere are outside this local history.

The unfollow settings here are local budgets. X prohibits scripting its website and automated bulk unfollowing, and randomized pacing cannot guarantee avoiding detection or enforcement. [X follow limits](https://help.x.com/en/using-x/x-follow-limit) · [X automation rules](https://help.x.com/en/rules-and-policies/x-automation)
