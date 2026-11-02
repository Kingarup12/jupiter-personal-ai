"""Privacy-first, local-preprocessing video understanding for Jupiter.

This module intentionally separates local media inspection from semantic model
inference.  A video is private by default: the generic chat route never
receives its raw bytes, no person is enrolled or identified, and Bytez is not
eligible for a private video.  A later, explicitly approved private specialist
executor can be supplied through the narrow callback interfaces below.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from PIL import Image, ImageStat


PIPELINE_VERSION = "video-understanding-v1"
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_DURATION_SECONDS = 15 * 60
MAX_KEYFRAMES = 24
MAX_PROVIDER_CALLS = 3
SUPPORTED_VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/x-m4v"})

_SPEECH_TERMS = ("say", "said", "speech", "spoken", "talk", "voice", "transcript", "hear", "audio")
_PEOPLE_TERMS = ("people", "person", "face", "faces", "who", "someone", "everyone", "man", "woman")
_SUMMARY_TERMS = ("summary", "summarize", "overview", "happens", "happening", "whole video", "entire video")
_IDENTITY_TERMS = ("who is", "identify", "identity", "recognize", "is this", "is that")


class VideoUnderstandingError(ValueError):
    """Raised only for invalid local video inputs, never provider failures."""


@dataclass(frozen=True)
class VideoMetadata:
    mime_type: str
    size_bytes: int
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    has_audio: bool | None = None
    local_probe_available: bool = False


@dataclass(frozen=True)
class QuestionAwarePlan:
    question_kind: str
    target_seconds: tuple[float, ...] = ()
    sample_budget: int = 8
    needs_speech: bool = False
    needs_people_detection: bool = False
    identity_requested: bool = False
    broad_summary: bool = False


@dataclass(frozen=True)
class FrameSample:
    timestamp_seconds: float
    fingerprint: str
    luminance: float | None = None
    image_path: str | None = None
    face_boxes: tuple[tuple[float, float, float, float], ...] = ()


@dataclass(frozen=True)
class FaceObservation:
    timestamp_seconds: float
    box: tuple[float, float, float, float]
    confidence: float = 0.0


@dataclass(frozen=True)
class PersonTrack:
    label: str
    observations: tuple[FaceObservation, ...]


@dataclass(frozen=True)
class TimelineEvent:
    start_seconds: float
    end_seconds: float
    kind: str
    description: str
    source: str = "local"
    actors: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    scene_id: str | None = None
    confidence: float | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecialistRoutePlan:
    privacy_class: str = "PRIVATE"
    maximum_provider_calls: int = MAX_PROVIDER_CALLS
    audio_route: tuple[str, ...] = ("groq:whisper-large-v3-turbo", "groq:whisper-large-v3")
    semantic_route: tuple[str, ...] = ("private-approved-video-specialist",)
    bytez_status: str = "blocked-private-video"
    bytez_catalog_state: str = "UNKNOWN"
    normal_router_required: bool = True
    text_to_video_status: str = "catalog-candidate-only"


@dataclass(frozen=True)
class VideoAnalysis:
    file_hash: str
    metadata: VideoMetadata
    plan: QuestionAwarePlan
    samples: tuple[FrameSample, ...] = ()
    scenes: tuple[float, ...] = ()
    people_tracks: tuple[PersonTrack, ...] = ()
    transcript: str = ""
    timeline: tuple[TimelineEvent, ...] = ()
    warnings: tuple[str, ...] = ()
    cache_hit: bool = False
    provider_calls: int = 0
    face_detector_status: str = "NOT_REQUESTED"
    specialist_plan: SpecialistRoutePlan = field(default_factory=SpecialistRoutePlan)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def structured_result(self) -> dict[str, Any]:
        """Provider-neutral, bounded result shape for future video specialists."""

        def event_record(event: TimelineEvent) -> dict[str, Any]:
            return {
                "start_ms": round(event.start_seconds * 1000),
                "end_ms": round(event.end_seconds * 1000),
                "actors": list(event.actors),
                "action": event.description if event.kind not in {"scene_change", "audio_event"} else None,
                "objects": list(event.objects),
                "scene_id": event.scene_id,
                "confidence": event.confidence,
                "evidence": list(event.evidence),
                "kind": event.kind,
                "source": event.source,
            }

        return {
            "video_hash": self.file_hash,
            "analysis_version": PIPELINE_VERSION,
            "metadata": asdict(self.metadata),
            "scenes": list(self.scenes),
            "keyframes": [asdict(sample) for sample in self.samples],
            "face_detections": [
                {"timestamp_ms": round(item.timestamp_seconds * 1000), "box": list(item.box), "confidence": item.confidence}
                for track in self.people_tracks for item in track.observations
            ],
            "people_tracks": [
                {
                    "track_id": track.label,
                    "first_seen_ms": round(track.observations[0].timestamp_seconds * 1000) if track.observations else None,
                    "last_seen_ms": round(track.observations[-1].timestamp_seconds * 1000) if track.observations else None,
                    "observations": len(track.observations),
                }
                for track in self.people_tracks
            ],
            "face_detector_status": self.face_detector_status,
            "objects": sorted({name for event in self.timeline for name in event.objects}),
            "transcript_segments": (
                [{"start_ms": None, "end_ms": None, "text": self.transcript}] if self.transcript else []
            ),
            "audio_events": [event_record(event) for event in self.timeline if event.kind == "audio_event"],
            "events": [event_record(event) for event in self.timeline],
            "warnings": list(self.warnings),
            "provider_evidence": [],
        }

    def to_reasoning_evidence(self) -> str:
        """Return conservative facts for Jupiter's final text-only answer.

        The text explicitly distinguishes local structural evidence from a
        semantic model result.  It gives a response model no grounds to invent
        objects, actions, speech, or personal identities when no approved
        specialist was run.
        """

        metadata = self.metadata
        lines = [
            "[Jupiter private video-inspection evidence]",
            "The original video remains private and was not sent to a generic model route.",
            f"Local probe available: {'yes' if metadata.local_probe_available else 'no'}.",
            f"Duration seconds: {metadata.duration_seconds if metadata.duration_seconds is not None else 'unknown'}.",
            f"Sampled timestamps: {', '.join(f'{sample.timestamp_seconds:.2f}' for sample in self.samples) or 'none'}.",
            f"Detected scene-change candidates: {', '.join(f'{second:.2f}' for second in self.scenes) or 'none'}.",
            "Face policy: people can only be described with anonymous continuity labels such as Person A; no stranger was enrolled or identified.",
            "A user-provided name is semantic context only and was not used for face recognition.",
        ]
        if self.people_tracks:
            lines.append("Anonymous tracks: " + ", ".join(
                f"{track.label} ({len(track.observations)} observation{'s' if len(track.observations) != 1 else ''})"
                for track in self.people_tracks
            ))
        if self.transcript:
            lines.append("Speech transcript from an explicitly approved transcription route:")
            lines.append(self.transcript[:12000])
        relevant_timeline = self._relevant_timeline()
        if relevant_timeline:
            lines.append("Timeline events:")
            lines.extend(
                f"- {event.start_seconds:.2f}-{event.end_seconds:.2f}s [{event.kind}/{event.source}]: {event.description}"
                for event in relevant_timeline
            )
        if self.warnings:
            lines.append("Limits: " + "; ".join(self.warnings))
        return "\n".join(lines)

    def _relevant_timeline(self) -> tuple[TimelineEvent, ...]:
        if self.plan.broad_summary or not self.plan.target_seconds:
            return self.timeline[:16]
        return tuple(
            event for event in self.timeline
            if any(abs(event.start_seconds - target) <= 8.0 for target in self.plan.target_seconds)
        )[:12]


def _data_root() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    configured = os.getenv("JUPITER_DATA_DIR")
    return Path(configured) if configured else project_root / "data"


def _user_scope(user_id: str) -> str:
    return hashlib.sha256(str(user_id or "").strip().casefold().encode("utf-8")).hexdigest()[:32]


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _parse_frame_rate(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        numerator, denominator = str(raw).split("/", 1)
        return float(numerator) / float(denominator) if float(denominator) else None
    except (TypeError, ValueError, ZeroDivisionError):
        return _safe_float(raw)


def _read_video_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class LocalVideoBackend:
    """Optional ffprobe/ffmpeg and local image feature extraction only."""

    def probe(self, path: Path, mime_type: str) -> VideoMetadata:
        size_bytes = path.stat().st_size
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return VideoMetadata(mime_type=mime_type, size_bytes=size_bytes)
        try:
            completed = subprocess.run(
                [
                    ffprobe, "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type,width,height,avg_frame_rate",
                    "-of", "json", str(path),
                ],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            if completed.returncode != 0:
                return VideoMetadata(mime_type=mime_type, size_bytes=size_bytes)
            payload = json.loads(completed.stdout or "{}")
            streams = payload.get("streams") if isinstance(payload, dict) else []
            streams = streams if isinstance(streams, list) else []
            video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
            audio = any(stream.get("codec_type") == "audio" for stream in streams)
            duration = _safe_float((payload.get("format") or {}).get("duration"))
            return VideoMetadata(
                mime_type=mime_type,
                size_bytes=size_bytes,
                duration_seconds=duration,
                width=int(video["width"]) if str(video.get("width") or "").isdigit() else None,
                height=int(video["height"]) if str(video.get("height") or "").isdigit() else None,
                frame_rate=_parse_frame_rate(video.get("avg_frame_rate")),
                has_audio=audio,
                local_probe_available=True,
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            return VideoMetadata(mime_type=mime_type, size_bytes=size_bytes)

    def extract_frames(
        self, path: Path, timestamps: Iterable[float], *, detect_faces: bool = False
    ) -> tuple[FrameSample, ...]:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return ()
        temporary_dir = Path(tempfile.mkdtemp(prefix="jupiter-video-frames-"))
        samples: list[FrameSample] = []
        try:
            for index, timestamp in enumerate(timestamps):
                output = temporary_dir / f"frame-{index:02d}.jpg"
                completed = subprocess.run(
                    [
                        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}",
                        "-i", str(path), "-frames:v", "1", "-vf",
                        "scale=640:-2:force_original_aspect_ratio=decrease", "-q:v", "4", "-y", str(output),
                    ],
                    capture_output=True,
                    timeout=18,
                    check=False,
                )
                if completed.returncode != 0 or not output.is_file():
                    continue
                samples.append(self._frame_sample(timestamp, output, detect_faces=detect_faces))
        finally:
            # Caller-visible samples contain no path to a retained image.  The
            # directory is deleted after local feature extraction, so raw video
            # frames are never placed in the durable analysis cache.
            shutil.rmtree(temporary_dir, ignore_errors=True)
        return tuple(samples)

    @staticmethod
    def _frame_sample(timestamp: float, path: Path, *, detect_faces: bool) -> FrameSample:
        raw = path.read_bytes()
        with Image.open(path) as image:
            image = image.convert("L").resize((32, 32))
            luminance = float(ImageStat.Stat(image).mean[0])
            pixels = list(image.getdata())
        mean = sum(pixels) / max(1, len(pixels))
        bits = "".join("1" if value >= mean else "0" for value in pixels)
        fingerprint = hashlib.sha256(bits.encode("ascii")).hexdigest()[:32]
        # Include a content component so uniformly lit but distinct frames do
        # not all collapse into one scene candidate.
        fingerprint = hashlib.sha256((fingerprint + hashlib.sha256(raw).hexdigest()[:16]).encode("ascii")).hexdigest()[:32]
        return FrameSample(
            timestamp_seconds=round(float(timestamp), 3),
            fingerprint=fingerprint,
            luminance=luminance,
            face_boxes=LocalFaceDetector.detect_path(path) if detect_faces else (),
        )

    def extract_audio(self, path: Path) -> Path | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        temporary_dir = Path(tempfile.mkdtemp(prefix="jupiter-video-audio-"))
        output = temporary_dir / "audio.wav"
        try:
            completed = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-y", str(output)],
                capture_output=True,
                timeout=35,
                check=False,
            )
            return output if completed.returncode == 0 and output.is_file() else None
        except (OSError, subprocess.SubprocessError):
            return None


class LocalFaceDetector:
    """Best-effort local detector; it produces geometry, never identity."""

    def detect(self, sample: FrameSample) -> tuple[FaceObservation, ...]:
        return tuple(
            FaceObservation(sample.timestamp_seconds, box, confidence=0.5)
            for box in sample.face_boxes
        )

    @staticmethod
    def status() -> str:
        try:
            import cv2

            cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            return "AVAILABLE" if cascade_path.is_file() else "UNAVAILABLE"
        except Exception:
            return "UNAVAILABLE"

    @staticmethod
    def detect_path(path: Path) -> tuple[tuple[float, float, float, float], ...]:
        """Use OpenCV's bundled Haar cascade only when it is locally present."""

        try:
            import cv2  # Optional local dependency; no network or provider use.

            image = cv2.imread(str(path))
            cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(str(cascade_path))
            if image is None or cascade.empty():
                return ()
            height, width = image.shape[:2]
            if not height or not width:
                return ()
            boxes = cascade.detectMultiScale(
                cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), scaleFactor=1.15, minNeighbors=5
            )
            return tuple(
                (round(float(x) / width, 5), round(float(y) / height, 5),
                 round(float(box_width) / width, 5), round(float(box_height) / height, 5))
                for x, y, box_width, box_height in boxes
            )
        except Exception:
            return ()


class VideoAnalysisCache:
    """User-scoped cache keyed by content hash and pipeline version only."""

    def __init__(self, root: Path | None = None):
        self.root = root or (_data_root() / "video_analysis")

    def _path(self, user_id: str, file_hash: str) -> Path:
        return self.root / _user_scope(user_id) / f"{file_hash}-{PIPELINE_VERSION}.json"

    def load(self, user_id: str, file_hash: str) -> dict[str, Any] | None:
        path = self._path(user_id, file_hash)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("pipeline_version") != PIPELINE_VERSION:
            return None
        if payload.get("file_hash") != file_hash or payload.get("user_scope") != _user_scope(user_id):
            return None
        return payload

    def save(self, user_id: str, analysis: VideoAnalysis, *, conversation_id: str | None = None) -> None:
        path = self._path(user_id, analysis.file_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.load(user_id, analysis.file_hash) or {}
        conversation_ids = set(existing.get("conversation_ids") or [])
        if conversation_id:
            conversation_ids.add(str(conversation_id))
        payload = {
            "pipeline_version": PIPELINE_VERSION,
            "user_scope": _user_scope(user_id),
            "file_hash": analysis.file_hash,
            "conversation_ids": sorted(conversation_ids),
            # No extracted frame, source path, original video byte, provider
            # raw blob, or face template is stored here. A future approved
            # transcript is private, user-scoped text and can be reused.
            "metadata": asdict(analysis.metadata),
            "samples": [asdict(sample) for sample in analysis.samples],
            "scenes": list(analysis.scenes),
            "people_tracks": [
                {"label": track.label, "observations": [asdict(item) for item in track.observations]}
                for track in analysis.people_tracks
            ],
            "people_detection_completed": analysis.plan.needs_people_detection,
            "face_detector_status": analysis.face_detector_status,
            "transcript": analysis.transcript,
            "timeline": [asdict(event) for event in analysis.timeline if event.source == "local"],
            "warnings": list(analysis.warnings),
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def delete_conversation(self, user_id: str, conversation_id: str) -> int:
        """Remove cache records whose only recorded source chat was deleted."""

        directory = self.root / _user_scope(user_id)
        removed = 0
        for path in directory.glob(f"*-{PIPELINE_VERSION}.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            conversations = {str(item) for item in payload.get("conversation_ids") or []}
            if str(conversation_id) not in conversations:
                continue
            conversations.discard(str(conversation_id))
            if conversations:
                payload["conversation_ids"] = sorted(conversations)
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        return removed

    def delete_file(self, user_id: str, file_hash: str) -> bool:
        """Remove the exact user-scoped cached analysis for deleted source bytes."""

        normalized = str(file_hash or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            return False
        path = self._path(user_id, normalized)
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed


class VideoQuestionPlanner:
    """Choose the smallest useful local sample set for the user's question."""

    @staticmethod
    def plan(question: str, metadata: VideoMetadata) -> QuestionAwarePlan:
        normalized = " ".join(str(question or "").casefold().split())
        targets = tuple(VideoQuestionPlanner._timestamp_targets(normalized, metadata.duration_seconds))
        needs_speech = any(term in normalized for term in _SPEECH_TERMS)
        needs_people = any(term in normalized for term in _PEOPLE_TERMS)
        identity_requested = any(term in normalized for term in _IDENTITY_TERMS)
        summary = any(term in normalized for term in _SUMMARY_TERMS)
        kind = "timestamp" if targets else "speech" if needs_speech else "people" if needs_people else "summary" if summary else "general"
        budget = 16 if summary else 12 if needs_people else 10 if needs_speech else 8
        if targets:
            budget = min(MAX_KEYFRAMES, max(6, 3 * len(targets) + 4))
        return QuestionAwarePlan(
            question_kind=kind,
            target_seconds=targets,
            sample_budget=min(MAX_KEYFRAMES, budget),
            needs_speech=needs_speech and metadata.has_audio is not False,
            needs_people_detection=needs_people,
            identity_requested=identity_requested,
            broad_summary=summary,
        )

    @staticmethod
    def _timestamp_targets(question: str, duration: float | None) -> list[float]:
        results: list[float] = []
        for match in re.finditer(r"\b(?:(\d{1,2}):)?(\d{1,2})(?::(\d{1,2}))?\b", question):
            first, second, third = match.groups()
            if third is not None:
                value = int(first or 0) * 3600 + int(second) * 60 + int(third)
            elif first is not None:
                value = int(first) * 60 + int(second)
            else:
                # A bare number is only a timestamp when nearby wording makes
                # that interpretation likely; ordinary questions use no target.
                prefix = question[max(0, match.start() - 16):match.start()]
                if not any(word in prefix for word in ("at ", "around ", "timestamp", "second", "time ")):
                    continue
                value = int(second)
            if duration is None or value <= duration:
                results.append(float(value))
        return list(dict.fromkeys(results))[:4]

    @staticmethod
    def sample_timestamps(metadata: VideoMetadata, plan: QuestionAwarePlan) -> tuple[float, ...]:
        duration = metadata.duration_seconds
        if duration is None or duration <= 0:
            return plan.target_seconds or (0.0,)
        duration = min(duration, float(MAX_VIDEO_DURATION_SECONDS))
        requested: list[float] = []
        if plan.target_seconds:
            for target in plan.target_seconds:
                requested.extend((target - 2.0, target, target + 2.0))
        remaining = max(0, plan.sample_budget - len(requested))
        if remaining:
            # Include both endpoints and evenly distributed interior samples.
            requested.extend(duration * index / max(1, remaining - 1) for index in range(remaining))
        return tuple(sorted({round(min(duration, max(0.0, value)), 3) for value in requested}))[:MAX_KEYFRAMES]


def _fingerprint_distance(first: str, second: str) -> float:
    if not first or not second:
        return 1.0
    bits = min(len(first), len(second)) * 4
    if not bits:
        return 1.0
    return sum(left != right for left, right in zip(bytes.fromhex(first), bytes.fromhex(second))) / bits


def detect_scene_boundaries(samples: Iterable[FrameSample]) -> tuple[float, ...]:
    ordered = sorted(samples, key=lambda item: item.timestamp_seconds)
    boundaries: list[float] = []
    for before, current in zip(ordered, ordered[1:]):
        fingerprint_change = _fingerprint_distance(before.fingerprint, current.fingerprint)
        luminance_change = abs((before.luminance or 0.0) - (current.luminance or 0.0)) / 255.0
        if max(fingerprint_change, luminance_change) >= 0.27:
            boundaries.append(current.timestamp_seconds)
    return tuple(boundaries)


def _iou(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if not intersection:
        return 0.0
    first_area = max(0.0, first[2]) * max(0.0, first[3])
    second_area = max(0.0, second[2]) * max(0.0, second[3])
    return intersection / max(1e-9, first_area + second_area - intersection)


class AnonymousFaceTracker:
    """Tracks face geometry as Person A/B; never maps it to an identity."""

    @staticmethod
    def track(observations: Iterable[FaceObservation]) -> tuple[PersonTrack, ...]:
        tracks: list[list[FaceObservation]] = []
        for observation in sorted(observations, key=lambda item: item.timestamp_seconds):
            matches = [(_iou(track[-1].box, observation.box), index) for index, track in enumerate(tracks) if track]
            score, index = max(matches, default=(0.0, -1))
            if score >= 0.28:
                tracks[index].append(observation)
            else:
                tracks.append([observation])
        return tuple(
            PersonTrack(label=f"Person {chr(65 + index)}", observations=tuple(track))
            for index, track in enumerate(tracks)
        )


def _metadata_from_cache(raw: dict[str, Any]) -> VideoMetadata:
    allowed = {name: raw.get(name) for name in VideoMetadata.__dataclass_fields__}
    return VideoMetadata(**allowed)


def _analysis_from_cache(
    payload: dict[str, Any], plan: QuestionAwarePlan, specialist_plan: SpecialistRoutePlan
) -> VideoAnalysis:
    samples = tuple(FrameSample(**item) for item in payload.get("samples", []) if isinstance(item, dict))
    tracks = []
    for raw_track in payload.get("people_tracks", []):
        if not isinstance(raw_track, dict):
            continue
        observations = tuple(FaceObservation(**item) for item in raw_track.get("observations", []) if isinstance(item, dict))
        tracks.append(PersonTrack(label=str(raw_track.get("label") or "Person"), observations=observations))
    timeline = tuple(TimelineEvent(**item) for item in payload.get("timeline", []) if isinstance(item, dict))
    return VideoAnalysis(
        file_hash=str(payload["file_hash"]),
        metadata=_metadata_from_cache(payload.get("metadata") or {}),
        plan=plan,
        samples=samples,
        scenes=tuple(float(item) for item in payload.get("scenes", [])),
        people_tracks=tuple(tracks),
        transcript=str(payload.get("transcript") or ""),
        timeline=timeline,
        warnings=tuple(str(item) for item in payload.get("warnings", [])),
        cache_hit=True,
        face_detector_status=str(payload.get("face_detector_status") or "NOT_REQUESTED"),
        specialist_plan=specialist_plan,
    )


class VideoUnderstandingPipeline:
    """Unifies local video steps and bounded optional private specialist hooks."""

    def __init__(self, backend: LocalVideoBackend | None = None, cache: VideoAnalysisCache | None = None):
        self.backend = backend or LocalVideoBackend()
        self.cache = cache or VideoAnalysisCache()

    @staticmethod
    def specialist_plan() -> SpecialistRoutePlan:
        """Read Bytez catalog state only; discovery and inference stay disabled."""

        catalog_path = Path(__file__).resolve().parents[1] / "config" / "bytez_model_catalog.json"
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            state = str(payload.get("state") or "UNKNOWN")
        except (OSError, ValueError, json.JSONDecodeError):
            state = "UNAVAILABLE"
        return SpecialistRoutePlan(
            # Private uploaded video is categorically not eligible for the
            # current Bytez policy even if a future metadata-only catalog has
            # a seemingly fitting free candidate.
            bytez_status="blocked-private-video",
            bytez_catalog_state=state,
        )

    def analyze(
        self,
        *,
        user_id: str,
        video_path: Path,
        mime_type: str,
        question: str,
        allow_cache: bool = True,
        conversation_id: str | None = None,
        face_detector: Callable[[FrameSample], Iterable[FaceObservation]] | None = None,
        transcriber: Callable[[Path], str] | None = None,
        semantic_analyzer: Callable[[tuple[FrameSample, ...], QuestionAwarePlan], Iterable[TimelineEvent]] | None = None,
    ) -> VideoAnalysis:
        path = Path(video_path)
        if mime_type not in SUPPORTED_VIDEO_MIMES:
            raise VideoUnderstandingError("Unsupported video type.")
        if not path.is_file():
            raise VideoUnderstandingError("Video is unavailable.")
        if path.stat().st_size > MAX_VIDEO_BYTES:
            raise VideoUnderstandingError("Videos must be 100 MB or smaller.")
        file_hash = _read_video_hash(path)
        metadata = self.backend.probe(path, mime_type)
        if metadata.duration_seconds and metadata.duration_seconds > MAX_VIDEO_DURATION_SECONDS:
            raise VideoUnderstandingError("Videos longer than 15 minutes are not supported yet.")
        plan = VideoQuestionPlanner.plan(question, metadata)
        specialist_plan = self.specialist_plan()
        cached = self.cache.load(user_id, file_hash) if allow_cache else None
        if cached and self._cache_covers_targets(cached, plan, transcriber_available=transcriber is not None):
            cached_analysis = _analysis_from_cache(cached, plan, specialist_plan)
            if conversation_id:
                self.cache.save(user_id, cached_analysis, conversation_id=conversation_id)
            return cached_analysis

        samples = self.backend.extract_frames(
            path, VideoQuestionPlanner.sample_timestamps(metadata, plan),
            detect_faces=plan.needs_people_detection,
        )
        warnings: list[str] = []
        if not metadata.local_probe_available:
            warnings.append("Local media probe is unavailable; duration and audio availability could not be verified.")
        if not samples:
            warnings.append("No local keyframes were extracted. No visual facts were inferred.")
        scenes = detect_scene_boundaries(samples)
        observations: list[FaceObservation] = []
        face_detector_status = "NOT_REQUESTED"
        if plan.needs_people_detection and face_detector is not None:
            face_detector_status = "INJECTED_PRIVATE"
            for sample in samples:
                observations.extend(face_detector(sample))
        elif plan.needs_people_detection:
            local_detector = LocalFaceDetector()
            face_detector_status = local_detector.status()
            for sample in samples:
                observations.extend(local_detector.detect(sample))
            if face_detector_status != "AVAILABLE":
                warnings.append("Local face/person detector is unavailable; no remote identity or face provider was used.")
        if plan.identity_requested:
            warnings.append("Identity recognition and enrollment are disabled unless the existing consented biometric profile flow is explicitly used.")
        tracks = AnonymousFaceTracker.track(observations)
        timeline: list[TimelineEvent] = [
            TimelineEvent(second, second, "scene_change", "Local frame-change candidate.", evidence=("local-frame-fingerprint",)) for second in scenes
        ]
        provider_calls = 0
        transcript = ""
        if plan.needs_speech and transcriber is not None and metadata.has_audio is not False:
            audio_path = self.backend.extract_audio(path)
            if audio_path is not None:
                try:
                    transcript = str(transcriber(audio_path) or "").strip()[:12000]
                    provider_calls += 1
                finally:
                    shutil.rmtree(audio_path.parent, ignore_errors=True)
            else:
                warnings.append("Audio extraction was unavailable; no speech transcript was inferred.")
        if semantic_analyzer is not None and provider_calls < MAX_PROVIDER_CALLS:
            for event in semantic_analyzer(samples, plan):
                if provider_calls >= MAX_PROVIDER_CALLS:
                    break
                if isinstance(event, TimelineEvent):
                    timeline.append(event)
            provider_calls += 1
        analysis = VideoAnalysis(
            file_hash=file_hash,
            metadata=metadata,
            plan=plan,
            samples=tuple(samples),
            scenes=scenes,
            people_tracks=tracks,
            transcript=transcript,
            timeline=tuple(sorted(timeline, key=lambda item: (item.start_seconds, item.end_seconds, item.kind))),
            warnings=tuple(dict.fromkeys(warnings)),
            provider_calls=provider_calls,
            face_detector_status=face_detector_status,
            specialist_plan=specialist_plan,
        )
        if allow_cache:
            self.cache.save(user_id, analysis, conversation_id=conversation_id)
        return analysis

    @staticmethod
    def _cache_covers_targets(
        cached: dict[str, Any], plan: QuestionAwarePlan, *, transcriber_available: bool
    ) -> bool:
        if plan.needs_people_detection and not bool(cached.get("people_detection_completed")):
            return False
        if plan.needs_speech and transcriber_available and not str(cached.get("transcript") or "").strip():
            return False
        if not plan.target_seconds:
            return True
        samples = cached.get("samples") if isinstance(cached.get("samples"), list) else []
        sample_seconds = [float(item.get("timestamp_seconds")) for item in samples if isinstance(item, dict) and _safe_float(item.get("timestamp_seconds")) is not None]
        return all(any(abs(second - target) <= 2.1 for second in sample_seconds) for target in plan.target_seconds)


__all__ = [
    "AnonymousFaceTracker", "FaceObservation", "FrameSample", "LocalFaceDetector", "LocalVideoBackend", "MAX_KEYFRAMES",
    "MAX_PROVIDER_CALLS", "MAX_VIDEO_BYTES", "MAX_VIDEO_DURATION_SECONDS", "PIPELINE_VERSION",
    "QuestionAwarePlan", "SUPPORTED_VIDEO_MIMES", "SpecialistRoutePlan", "TimelineEvent", "VideoAnalysis",
    "VideoAnalysisCache", "VideoMetadata", "VideoQuestionPlanner", "VideoUnderstandingError", "VideoUnderstandingPipeline",
    "detect_scene_boundaries",
]
