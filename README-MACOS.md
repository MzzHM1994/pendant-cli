# Save your Pendant recordings on a Mac

Limitless is shutting down the cloud in December 2026. The transcripts
that used to live on their servers can live on your Mac instead. This
guide gets you there. You do not need to be a programmer.

When it is working, one command will:

1. Copy the recordings off the pendant.
2. Put each one in a folder named by the day it was recorded.
3. Write a transcript next to the audio, on your Mac.

The transcript is three files:

- a plain text file (`.txt`) that starts with the date
- a subtitle file (`.srt`) with timestamps
- a `.json` file with the same timestamps, if you ever want a program to read it

Nothing in that command sends your audio to the internet. Transcription
uses a program called whisper.cpp that runs on the Mac.

**Never run these commands.** They can drop the Bluetooth pairing or
erase what is on the pendant:

```text
pendant unpair
pendant reset
pendant clear-storage
pendant factory-reset
```

The sync command in this guide is not one of those. Leave them alone.

## What you need

- A Mac with an Apple silicon chip (M1, M2, M3, or M4). A Mac mini is fine.
- The pendant, charged, and close to the Mac.
- Bluetooth turned on.
- About 500 MB of free disk for the recommended speech model, plus room for your recordings.

## 1. Install Homebrew

Homebrew is a tool that installs the other programs. Open **Terminal**
(search for it with Spotlight) and paste this, then press Return:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the prompts. At the end the installer prints two lines that start
with `eval`. If Terminal later says `brew: command not found`, paste
those two lines again.

## 2. Install Python, ffmpeg, and whisper.cpp

Paste this:

```bash
brew install python@3.12 ffmpeg whisper.cpp
```

- **Python** runs this tool.
- **ffmpeg** turns each recording into a form whisper can read.
- **whisper.cpp** is the speech-to-text program. On an M-series Mac its
  Homebrew build uses the Mac's graphics chip (Metal), so it is quick
  and it stays on the machine. You do not install a separate AI app.

## 3. Download a speech model (one time)

whisper.cpp needs a model file. This download is the only step that
uses the internet for transcription. After the file is on the Mac,
transcribing a recording does not go online.

The recommended size is **small**. It is accurate enough for normal
conversation and comfortable on a Mac mini. Other sizes:

| Size you type | What it is like |
|---|---|
| `tiny` | Fastest, more mistakes |
| `base` | A bit better, still light |
| `small` | Recommended |
| `medium` | Slower, a bit more accurate, about 1.5 GB |
| `large-v3` | Slowest, largest download, about 3 GB |

From the folder that contains this project (the `pendant-cli` folder), run:

```bash
bash scripts/download-whisper-model.sh small
```

The file is saved in your home folder under
`Library/Application Support/pendant-cli/models/`. You can copy that
file from another computer if you would rather not download it on this Mac.

To use a different size later, download it the same way and add
`--model medium` (or whichever size) to the sync command.

## 4. Install this tool

In Terminal, go to the project folder. If you have not downloaded it yet,
the person who set up the fork can tell you where it is. Then:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python scripts/gen_proto.py
```

`source .venv/bin/activate` is needed in each new Terminal window before
the commands below. You will know it worked because the start of the
line shows `(.venv)`.

You can skip anything called an "opus extra". Transcription uses ffmpeg,
which you already installed.

Check that the command exists:

```bash
pendant-cli --help
```

You should see a list of commands, including `sync-and-transcribe` and
`transcribe`.

## 5. Allow Bluetooth

macOS asks before a program may use Bluetooth.

1. Open **System Settings**.
2. Go to **Privacy & Security**, then **Bluetooth**.
3. Turn the switch on for **Terminal**. If you use another terminal app, turn it on for that app too.

Also open **System Settings → Bluetooth** and make sure Bluetooth itself
is on. The first time you scan, the Mac may show a permission prompt.
Allow it.

## 6. A read-only scan

This only listens for the pendant. It does not connect long enough to
download, and it does not delete anything.

```bash
pendant scan
```

If the pendant is awake and nearby, you get a line like:

```text
AA:BB:CC:DD:EE:FF
```

That is its Bluetooth address. Write it down. Yours will be different.
Every command below uses your real address in place of the example.

If it says `No pendant found`, see Troubleshooting. Do not run an
unpair or reset command to "fix" it.

## 7. Sync and transcribe

This is the command you will run when you want new recordings:

```bash
pendant-cli sync-and-transcribe AA:BB:CC:DD:EE:FF
```

Or, from the project folder:

```bash
make sync-and-transcribe ADDRESS=AA:BB:CC:DD:EE:FF
```

Or:

```bash
bash scripts/sync-and-transcribe.sh AA:BB:CC:DD:EE:FF
```

What you should see:

- a line that whisper.cpp and the **small** model are ready
- a download line
- `Filed 2026/10/2026-10-04/...opus` for each new recording
- `Transcribing ... with model small`
- `Transcribed ... -> .txt, .srt, .json`
- a last line that starts with `Done.`

The files go in a folder called `Pendant` in your home folder:

```text
Pendant/
  2026/
    10/
      2026-10-04/
        capture_rec0000.opus      the audio
        capture_rec0000.txt       the transcript, with the date at the top
        capture_rec0000.srt       the same words with timestamps
        capture_rec0000.json      the same words, for a spreadsheet or script
        capture.bin               the raw download
        capture.opus.manifest.json
```

Open the `.txt` file in TextEdit. The first line is `Date: 2026-10-04`.

A normal download saves each recording on the Mac and then lets the
pendant reuse that space for new audio. That is the ordinary sync this
tool already did. It is not an unpair, a reset, or a storage wipe.
Do not run those commands.

### Run it again

Run the same command whenever you want to collect new audio. It is safe
to repeat.

- A recording already in the dated folder is not copied a second time.
- A recording that already has a `.txt`, a `.srt`, and a `.json` is not transcribed again.
- New recordings are filed and transcribed.
- The command prints what it skipped and what it wrote.

If a transcript failed halfway, run the command again. Finished
transcripts stay put. Only the missing ones are tried again.

### Useful variations

Transcribe files you already have, without touching the pendant:

```bash
pendant-cli transcribe ~/Pendant
```

Use a different model:

```bash
pendant-cli sync-and-transcribe AA:BB:CC:DD:EE:FF --model base
```

Put the library somewhere else:

```bash
pendant-cli sync-and-transcribe AA:BB:CC:DD:EE:FF --library ~/Documents/Pendant
```

If the pendant encrypts audio and someone has given you a key file:

```bash
pendant-cli sync-and-transcribe AA:BB:CC:DD:EE:FF --key /path/to/key.pem
```

## Troubleshooting

**`brew: command not found`**
The Homebrew installer printed two `eval` lines. Paste them into
Terminal, or close Terminal and open it again.

**`pendant-cli: command not found`**
You are not in the project folder, or the virtual environment is off.
Run:

```bash
cd /path/to/pendant-cli
source .venv/bin/activate
```

**`Could not find whisper.cpp`**
Run `brew install whisper.cpp`. Then `whisper-cli -h` should print help.
If it does not, close Terminal, open it again, and retry.

**`Could not find a local whisper.cpp model`**
Run `bash scripts/download-whisper-model.sh small` from the project
folder. To point at a file you downloaded yourself:

```bash
pendant-cli sync-and-transcribe AA:BB:CC:DD:EE:FF --model ~/Downloads/ggml-small.bin
```

**`Could not find ffmpeg`**
Run `brew install ffmpeg`.

**`No pendant found`**
Turn Bluetooth on. Wake the pendant and set it next to the Mac. Quit
the Limitless phone app, and turn Bluetooth off on the phone so the
phone is not holding the connection. Run `pendant scan` again.

If it still does not appear, stop. Do not run `pendant unpair`,
`pendant reset`, `pendant clear-storage`, or `pendant factory-reset`.

**Bluetooth permission error, or the scan hangs**
System Settings → Privacy & Security → Bluetooth → Terminal on.
Quit Terminal completely (Command-Q) and open it again.

**The download starts and then stops with no recordings**
The pendant may not have new audio. That is a successful run with
nothing to file. Record something with the pendant and try later.
The read-only check is still `pendant scan`.

**Transcription is slow**
Use a smaller model: `--model tiny` or `--model base`. `small` is the
balance this guide recommends on a Mac mini.

**A transcript looks wrong**
You can redo one day without downloading again:

```bash
pendant-cli transcribe ~/Pendant/2026/10/2026-10-04 --force --model medium
```

`--force` replaces the transcript. It does not touch the audio file
and it does not talk to the pendant.

**`Done. ... failed`**
Read the `Failed ...` line above it. Fix that (often a missing model
or a damaged recording) and run the same command again. Work that
already finished is skipped.

## What this command will not do

`pendant-cli sync-and-transcribe` only downloads, files, and
transcribes. It does not upload audio. It does not unpair the pendant.
It does not clear the pendant's storage. It does not reset it.

The older commands `pendant push`, `pendant sync --upload`, and
`pendant enroll-voice` talk to a server. Do not use them if you want
the recordings to stay on this Mac.
