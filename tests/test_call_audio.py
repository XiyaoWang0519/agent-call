from __future__ import annotations

import base64

import pytest

from app.call_audio import AudioTrack, CallAudio

SILENCE = base64.b64encode(bytes([255]) * 160).decode()
VOICE = base64.b64encode(bytes([32, 160]) * 80).decode()
QUIET_NOISE = base64.b64encode(bytes([250, 122]) * 80).decode()


def test_short_noise_is_not_speech_and_silence_is_not_playback():
    track = AudioTrack()
    for ms in range(0, 500, 20):
        assert track.observe(ms, QUIET_NOISE, now=1) == (False, False)
    assert track.last_voice_ms is None
    assert track.silence_seconds(now=1) is None
    assert track.observe(500, VOICE, now=1) == (False, False)
    assert track.observe(520, SILENCE, now=1) == (False, False)
    assert track.speaking is False


def test_continuous_carrier_voice_then_silence_establishes_playback():
    track = AudioTrack()
    assert track.observe(0, VOICE, now=1) == (False, False)
    assert track.observe(20, VOICE, now=1.02) == (True, False)
    for ms in range(40, 320, 20):
        assert track.observe(ms, SILENCE, now=1 + ms / 1000) == (False, False)
    assert track.observe(320, SILENCE, now=1.32) == (False, True)
    assert track.silence_seconds(now=1.32) == pytest.approx(0.3)
    assert track.silence_seconds(now=2) is None


def test_gap_or_reordered_media_does_not_prove_silence():
    track = AudioTrack()
    track.observe(0, VOICE, now=1)
    track.observe(20, VOICE, now=1)
    track.observe(20, SILENCE, now=2)
    assert track.end_ms == 40
    assert track.received_at == 1
    track.observe(4000, SILENCE, now=5)
    assert track.silence_seconds(now=5) is None
    track.observe(4020, VOICE, now=5)
    track.observe(4040, VOICE, now=5)
    track.observe(4060, SILENCE, now=5)
    assert track.silence_seconds(now=5) == pytest.approx(0.02)


@pytest.mark.parametrize("timestamp,payload", [(-1, VOICE), (0, "not base64"), (0, "A" * 17000)])
def test_invalid_audio_is_rejected(timestamp, payload):
    with pytest.raises(ValueError):
        AudioTrack().observe(timestamp, payload, now=1)


def test_track_identity_is_required():
    with pytest.raises(ValueError):
        CallAudio().observe("both_tracks", 0, VOICE)
