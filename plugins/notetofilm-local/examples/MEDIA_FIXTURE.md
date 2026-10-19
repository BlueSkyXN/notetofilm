# Offline media fixture

This example uses an algorithmic PNG, FFmpeg test video, and synthetic sine tones.
It calls **no image/video AI service, EdgeTTS, or company API**. Its subtitles have
explicitly authored fixture times. They are not a speech-recognition result.

From the plugin directory:

```bash
python examples/create_media_fixture.py --output /tmp/notetofilm-fixture-example --render
```

Choose a new output directory. The recipe preserves an existing fixture instead
of overwriting it. The command returns JSON with the generated paths:

- `assets/`: original, explicitly synthetic inputs.
- `compose.json`: a complete reproducible compose manifest.
- `FIXTURE.json`: the origin and meaning of the test media.
- `output/video.mp4`: a real H.264/AAC MP4, approximately 4.6 seconds.
- `output/subtitles.srt`: sidecar subtitles, also burned into this example.
- `output/timeline.json`: actual input durations, trims, holds, audio choices,
  frame timing, and rendered media metadata.
- `output/segments/`: generated intermediates; original assets remain unchanged.

The four scenes exercise independent narration, native video audio, mixed audio,
and explicit silence. The first scene's `audio_policy: edge_tts` identifies the
independent-narration **composition path**. Its audio is a local test tone; it is
not an assertion that EdgeTTS produced that file.

## Compose manifest behavior

`output` accepts even `width`/`height`, numeric or fractional `fps`, and
`subtitles_mode: sidecar | burn | none` (default `sidecar`). Optional
`subtitle_font` is a plain font family name; `subtitle_font_size` is in ASS style
units. Font availability depends on the local machine.

Each scene has a stable `id`, a `visuals` list, and an `audio_policy`:

- `edge_tts`: use `audio.path` as independent narration; mute original video sound.
- `native`: use sound from selected video inputs; omit `audio.path`.
- `mix`: combine independent narration and native video sound. `native_volume`
  defaults to 0.25 and narration volume to 1. A limiter handles mixed peaks.
- `silent`: omit narration and mute all source audio.

`audio.start_seconds` delays narration within the scene. `audio.volume` or scene
`narration_volume` changes its level. `native_volume` controls native audio.

Visuals accept `path`, optional actual `kind`, `fit: contain | cover`,
`trim_start`, `duration_seconds`, and `hold: true`. A single still follows actual
narration length. A single video without its own duration can be trimmed to the
narration or explicit scene window. Extending past its available footage requires
`hold: true`; the last picture is frozen and missing native audio becomes silence.
It does not loop speech or video. Multiple visuals need durations, except that a
final still can fill a known remaining scene window.

An explicit scene duration cannot truncate narration. Output windows are rounded
to complete frames. `subtitles_path` supplies existing SRT or simple WebVTT cues;
there is no character-count timing fallback. Subtitle cues default to narration
time for `edge_tts`/`mix`, or scene-local time otherwise.
`subtitles_offset_seconds` overrides that scene-local offset. Whole-film offsets
are then added from the actual frame timeline. Cues outside the scene are rejected.
When using trimmed native footage, supply scene-local subtitle times or the
appropriate explicit offset.

The current renderer uses cuts, not motion graphics or generated transitions.
Exact charts/titles should be laid out into input artwork before composition.
Generated speech/video, lip-sync, and source separation are not simulated by the
fixture. Audio and video in intermediate MOV files use a common frame rate and PCM
audio; AAC is encoded once for the final MP4.

## EdgeTTS separately

Install optional `edge-tts==7.2.8` to call the real online service. The adapter saves
the actual audio, returned word/sentence boundary JSON, SRT when available, and
request metadata. A version-specific endpoint compatibility fix matches the
parent repository's `app/tts_compat.py`. Connection or service success depends on
the execution environment and is not demonstrated by this offline example.

If `speech_text` expands pronunciation, subtitles currently use the service's
returned text. They do not automatically restore a separately worded display
transcript. If the service provides no timing metadata, the adapter does not invent
subtitles.
