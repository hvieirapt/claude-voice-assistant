# Claude Voice Assistant

Talk to Claude Code by voice, inside the Claude desktop app, with everything running on your Mac. Only Claude itself
is in the cloud: speech recognition, text-to-speech, the voice-activity detector, the intent classifier and the
meeting-speaker identification are all local models on Apple Silicon.

## The premise

A developer running several Claude Code sessions should not have to read every screen or touch the keyboard to
know what is going on. One session becomes the **voice assistant**: it owns the microphone and the voice, and it
talks to you like a colleague. Ask it what the other sessions are doing, have it read you the question a session is
waiting on and answer by voice, send work to the right session, dictate a prompt, or run a meeting and get timed
minutes with speaker identification. Every other session stays quiet; the only thing that reaches you from them is
a permission request, asked aloud and answered with a "yes" or a "no".

## Features

- **Local speech recognition**: Whisper large-v3-turbo on MLX, with Parakeet for live partial text while you speak.
- **Local text-to-speech**: Apple system voices, Piper (178 voices in 58 languages, installed on first use) or
  Qwen3-TTS voice cloning from a 10-second sample. 28 interface languages; the system prompt asks Claude to answer
  in short spoken prose in the chosen language.
- **Hands-free assistant mode**: one designated session lists, reads and messages the other sessions through the
  app's session tools and summarises them for you.
- **Permissions by voice**: when any session asks for permission, the assistant says what kind of action it is
  (never the content) and the project it comes from; "yes" or "no" answers the native dialog. Ships inside the
  plugin as a `PreToolUse` hook, no setup.
- **Natural turn-taking**: speak over the voice to interrupt it; short acknowledgements do not interrupt work;
  "stop" and "skip" are understood in any language by a local classifier, not a word list.
- **Meeting mode**: `/voice-assistant meeting <title>` listens to the room, separates speakers by voice embedding, writes timed
  minutes, asks Claude for interim summaries and a formal record at the end.
- **A band above the prompt and a side pane**: state, microphone level, transcript bubbles, voice and mode
  selectors, settings (voice, speed, sensitivity, pause length, filler-word cleanup, barge-in, audio devices,
  voice search and install).

## Install

Requirements: macOS on Apple Silicon, Claude desktop app with the Code tab, and [uv](https://docs.astral.sh/uv/).
The first start downloads the models (a few GB) and takes a few minutes; the band shows the progress.

In the Claude desktop app: Settings → Plugins → Add marketplace → from a git repository, with
`https://github.com/hvieirapt/claude-voice-assistant`, then add the plugin **voz**.

From a terminal session of Claude Code:

```
/plugin install voz --marketplace hvieirapt/claude-voice-assistant
```

Then open the session you want to talk to and run `/voice-assistant`: it becomes the assistant and starts with the
voice on from then on. `/voice-assistant off` turns it off; `/voice-assistant` in another session moves the voice
there. Other arguments: `auto` or `manual` (record with the button), `meeting <title>[, N speakers]` and `end`.

## How it works

Microphone (never system audio) → Silero VAD → Whisper on MLX, Parakeet for live text → filler cleanup and a
substitutions dictionary → the prompt. Reply → sentences → TTS → speakers, streamed while Claude is still writing.
The Python daemon lives in `daemon/` and is installed by the plugin itself with `uv` on first use; the plugin talks
to it over stdout events and a small HTTP API on `localhost:8765`.

The interface language of the plugin's own labels is Portuguese; the spoken language is whatever you pick.

## Security model

- **Local API**: it listens on `127.0.0.1` only and serves its own panel, the plugin and the permission hook. Requests
  with another `Host` or `Origin`, or a `POST` that isn't JSON, are refused. Open the panel at `http://localhost:8765` or
  `http://127.0.0.1:8765`. There is no authentication, so other processes and other users on the same Mac can reach it.
- **Approving by voice**: only a short, clear yes approves. A refusal denies, and anything else falls back to the normal
  permission dialog. The voice doesn't check who is speaking, so anyone in the room can answer.
- **What you hear**: the question names the programs in a command, not their arguments; the full command is in the
  panel. Commands that can't be summarised with confidence go straight to the dialog. This is a best-effort reading of
  the command, not a shell parser, so check anything sensitive in the dialog.

## License

Public domain, under [The Unlicense](LICENSE).
