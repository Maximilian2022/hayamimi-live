# -*- coding: utf-8 -*-
"""Speaker Profile data model and serialization utilities for hayamimi."""

from __future__ import annotations

import datetime
import re
from typing import List, Optional
import numpy as np
from pydantic import BaseModel, Field, field_validator


class ExtractedSection(BaseModel):
    """Information about the audio section extracted from YouTube."""
    start_time: str = Field(..., description="Start timestamp in HH:MM:SS or MM:SS format")
    end_time: str = Field(..., description="End timestamp in HH:MM:SS or MM:SS format")
    duration_sec: float = Field(..., description="Duration of extracted section in seconds")


class ReplacementRule(BaseModel):
    """Individual word/phrase replacement rule."""
    pattern: str = Field(..., description="Misrecognized phrase or word")
    replacement: str = Field(..., description="Correct intended phrase or word")
    reason: Optional[str] = Field(None, description="Reason for replacement (articulation habit, game term, etc.)")
    hit_count: int = Field(0, description="Number of times matched in live transcription")
    last_hit_at: Optional[str] = Field(None, description="Last matched timestamp in ISO 8601")
    added_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(),
        description="Timestamp when rule was created",
    )
    source_video_id: Optional[str] = Field(None, description="YouTube Video ID from which this rule was extracted")
    is_archived: bool = Field(False, description="Whether this rule is archived (inactive) due to staleness")
    tags: List[str] = Field(
        default_factory=list,
        description="Associated tags (e.g. hashtags from video title: #ApexLegends, #スト6, or '共通')",
    )

    @field_validator("pattern", "replacement")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


def sanitize_and_trim_rule(pattern: str, replacement: str) -> Optional[tuple[str, str]]:
    """Sanitize, trim fillers/particles, and guard against overly long or short patterns.
    Returns (cleaned_pattern, cleaned_replacement) or None if unsafe/invalid.
    - Strips whitespace and quotes.
    - Rejects patterns shorter than 2 chars (e.g. 1-char particles like 'と' -> 'そ').
    - Trims common Japanese prefix fillers ('えーっと', 'あの') and leading/trailing particles.
    - Rejects patterns longer than 12 characters to prevent accidental sentence-level overrides.
    """
    if not pattern or not replacement:
        return None
    p = pattern.strip().strip("\"'「」『』【】")
    r = replacement.strip().strip("\"'「」『』【】")

    # Reject identical or empty
    if not p or not r or p == r:
        return None

    # Strip prefix fillers: えーっと, あのー, まあ, なんか etc.
    p = re.sub(r"^(?:えーっと|ええと|あのー|あの|えー|そのー|まぁ|まあ|なんか|うーん)\s*", "", p)
    # Strip leading/trailing particles if pattern is still long
    if len(p) > 4:
        p = re.sub(r"^[はがをにへとでもやの]\s*", "", p)
        p = re.sub(r"\s*[はがをにへとでもやの]$", "", p)
    p = p.strip()

    # Reject too short (single char)
    if len(p) < 2:
        return None

    # Reject overly long sentence-level phrases (> 12 chars)
    if len(p) > 12:
        return None

    return (p, r)


CURRENT_SCHEMA_VERSION = '1.2.0'

DEFAULT_COLLAB_KEYWORDS: List[str] = [
    "コラボ",
    "collab",
    "collaboration",
    "w/",
    "with",
    "ゲスト",
    "guest",
    "凸待ち",
    "逆凸",
    "対談",
    "参加型",
    "大会",
    "カスタム",
    "スクリム",
    "among us",
    "アモアス",
    "人狼",
    "feign",
]


class SpeakerProfile(BaseModel):
    """Complete speaker profile model."""
    schema_version: str = Field("1.2.0", description="Schema version")
    speaker_id: str = Field(..., description="Unique alphanumeric identifier (e.g. val_channel)")
    display_name: str = Field(..., description="Human-readable display name (e.g. ヴァル閣下)")
    created_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(),
        description="Creation timestamp (ISO 8601)",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(),
        description="Last update timestamp (ISO 8601)",
    )
    source_video_url: Optional[str] = Field(None, description="Source YouTube video URL")
    source_video_urls: List[str] = Field(
        default_factory=list,
        description="List of YouTube video URLs processed to train this profile",
    )
    youtube_channel_url: Optional[str] = Field(
        None,
        description="Associated YouTube Channel URL (for continuous automatic updates)",
    )
    processed_video_ids: List[str] = Field(
        default_factory=list,
        description="List of processed YouTube Video IDs to prevent duplicate training",
    )
    skipped_video_ids: List[str] = Field(
        default_factory=list,
        description="List of skipped YouTube Video IDs (e.g. shorts, music, non-speech)",
    )
    extracted_section: Optional[ExtractedSection] = Field(None, description="Extracted section info")
    articulation_traits: List[str] = Field(
        default_factory=list,
        description="List of identified pronunciation traits (e.g. サ行が甘くタ行に化けやすい)",
    )
    replacements: List[ReplacementRule] = Field(
        default_factory=list,
        description="List of active replacement rules",
    )
    archived_replacements: List[ReplacementRule] = Field(
        default_factory=list,
        description="List of archived (inactive) replacement rules due to staleness (>=180 days without hits)",
    )
    second_pass_hints: Optional[str] = Field(
        None,
        description="Context hints for 2nd-pass refinement models",
    )
    voiceprint: Optional[List[float]] = Field(
        None,
        description="L2-normalized 512-dimensional speaker embedding centroid (CAM++)",
    )
    voiceprint_threshold: float = Field(
        0.45,
        description="Cosine similarity threshold for speaker verification",
    )
    voiceprint_sample_count: int = Field(
        0,
        description="Total speech segments used to compute/refine this voiceprint centroid",
    )
    collab_filter_enabled: bool = Field(
        True,
        description="Whether to automatically exclude collaboration/guest archives during training & maintenance",
    )
    collab_exclude_keywords: List[str] = Field(
        default_factory=lambda: list(DEFAULT_COLLAB_KEYWORDS),
        description="Keywords used to detect and exclude collaboration or multi-speaker archives",
    )
    lora_adapter_path: Optional[str] = Field(
        None,
        description="Relative or absolute path to trained speaker LoRA adapter binary",
    )
    lora_trained_video_count: int = Field(
        0,
        description="Number of videos used to train/grow the speaker LoRA adapter",
    )

    def get_available_tags(self) -> List[str]:
        """Return sorted list of all unique tags present across active replacements."""
        tag_set = set()
        for r in self.replacements:
            if not r.is_archived and r.tags:
                for t in r.tags:
                    if t and isinstance(t, str):
                        tag_set.add(t.strip())
        tags = sorted(list(tag_set))
        if "共通" in tags:
            tags.remove("共通")
            tags.insert(0, "共通")
        return tags

    def get_active_replacements(self, active_tags: Optional[List[str]] = None) -> dict[str, str]:
        """Return mapping of pattern -> replacement filtered by active priority tags.
        If active_tags is None or empty: all active rules are included.
        If active_tags is provided:
          - Rules with no tags or with '共通' are ALWAYS included.
          - Rules with matching tags in active_tags are included.
        """
        if not active_tags:
            return {
                r.pattern: r.replacement
                for r in self.replacements
                if not r.is_archived and r.pattern and r.replacement
            }

        target_tags = {t.strip().lower() for t in active_tags if t and isinstance(t, str)}
        mapping = {}
        for r in self.replacements:
            if r.is_archived or not r.pattern or not r.replacement:
                continue
            rule_tags = [t.strip().lower() for t in (r.tags or []) if t]
            # Common rule: no tags or contains '共通'
            is_common = not rule_tags or any(t in ("共通", "#共通", "common", "all") for t in rule_tags)
            is_matched = any(t in target_tags for t in rule_tags)
            if is_common or is_matched:
                mapping[r.pattern] = r.replacement
        return mapping

    def migrate_from_legacy(self) -> bool:
        """Ensure backward compatibility for profiles created with older versions.
        - Upgrades schema_version to 1.2.0.
        - Ensures tags field exists on all rules (defaults to ['共通'] if empty).
        - Sets added_at of existing rules to created_at/updated_at to guarantee grace period.
        """
        changed = False
        is_legacy = (self.schema_version != "1.2.0")
        if is_legacy:
            self.schema_version = "1.2.0"
            changed = True

        if getattr(self, "collab_filter_enabled", None) is None:
            self.collab_filter_enabled = True
            changed = True
        if getattr(self, "collab_exclude_keywords", None) is None:
            self.collab_exclude_keywords = list(DEFAULT_COLLAB_KEYWORDS)
            changed = True

        fallback_dt = self.created_at or self.updated_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        for r in self.replacements + self.archived_replacements:
            if is_legacy or not getattr(r, "added_at", None):
                r.added_at = fallback_dt
                changed = True
            if getattr(r, "hit_count", None) is None:
                r.hit_count = 0
                changed = True
            if getattr(r, "is_archived", None) is None:
                r.is_archived = False
                changed = True
            if getattr(r, "tags", None) is None:
                r.tags = ["共通"]
                changed = True
            elif not r.tags:
                r.tags = ["共通"]
                changed = True

        return changed

    def archive_stale_rules(self, days: int = 180) -> int:
        """Archive rules that have not been hit for >= days (default 180 days / 6 months) and have hit_count == 0.
        Returns the number of rules moved to archived_replacements.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(days=days)

        active: List[ReplacementRule] = []
        to_archive: List[ReplacementRule] = []

        for r in self.replacements:
            # Check timestamp: prefer last_hit_at, then added_at
            ts_str = r.last_hit_at or r.added_at
            rule_dt = None
            if ts_str:
                try:
                    rule_dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    pass

            # Stale if never hit (hit_count == 0) AND older than cutoff
            if r.hit_count == 0 and rule_dt and rule_dt < cutoff:
                r.is_archived = True
                to_archive.append(r)
            else:
                active.append(r)

        if to_archive:
            self.replacements = active
            # Merge into archived_replacements without duplicate pattern
            archived_map = {ar.pattern: ar for ar in self.archived_replacements}
            for ar in to_archive:
                archived_map[ar.pattern] = ar
            self.archived_replacements = list(archived_map.values())
            self.updated_at = now.isoformat()

        return len(to_archive)

    def unarchive_rule(self, pattern: str) -> bool:
        """Unarchive a rule from archived_replacements back to active replacements."""
        found = None
        remaining: List[ReplacementRule] = []
        for ar in self.archived_replacements:
            if ar.pattern == pattern and found is None:
                found = ar
            else:
                remaining.append(ar)

        if found:
            self.archived_replacements = remaining
            found.is_archived = False
            found.added_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.replacements.append(found)
            self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return True
        return False

    def merge_profile(self, other: "SpeakerProfile") -> "SpeakerProfile":
        """Merge another profile's findings (from an additional YouTube video) into this profile.
        - Preserves existing rules and appends new unique rules.
        - Combines articulation traits without duplicates.
        - Merges voiceprint embeddings with weighted vector average and L2-normalization.
        - Keeps full training history of source video URLs and video IDs (prevents duplicate runs).
        - Merges channel URL and skipped video IDs.
        - Updates updated_at timestamp.
        """
        urls = list(self.source_video_urls)
        if self.source_video_url and self.source_video_url not in urls:
            urls.append(self.source_video_url)
        for u in other.source_video_urls:
            if u and u not in urls:
                urls.append(u)
        if other.source_video_url and other.source_video_url not in urls:
            urls.append(other.source_video_url)
        self.source_video_urls = urls
        if other.source_video_url:
            self.source_video_url = other.source_video_url

        # Merge YouTube channel URL if self doesn't have one
        if not self.youtube_channel_url and other.youtube_channel_url:
            self.youtube_channel_url = other.youtube_channel_url

        # Merge processed video IDs for duplicate prevention
        vids = list(self.processed_video_ids)
        for vid in other.processed_video_ids:
            if vid and vid not in vids:
                vids.append(vid)
        # Extract video IDs from all URLs
        for u in self.source_video_urls:
            m = re.search(r"(?:[?&]v=|youtu\.be/|/live/|/shorts/)([a-zA-Z0-9_-]{11})", u)
            if m and m.group(1) not in vids:
                vids.append(m.group(1))
        self.processed_video_ids = vids

        # Merge skipped video IDs
        skipped = set(self.skipped_video_ids)
        for svid in other.skipped_video_ids:
            if svid:
                skipped.add(svid)
        self.skipped_video_ids = list(skipped)

        existing_traits = list(self.articulation_traits)
        for t in other.articulation_traits:
            if t and t not in existing_traits:
                existing_traits.append(t)
        self.articulation_traits = existing_traits

        rule_map = {r.pattern: r for r in self.replacements}
        for new_r in other.replacements:
            if new_r.pattern in rule_map:
                cur = rule_map[new_r.pattern]
                # Merge tags without duplicates
                combined_tags = list(cur.tags or [])
                for t in (new_r.tags or []):
                    if t and t not in combined_tags:
                        combined_tags.append(t)
                combined_reason = cur.reason
                if new_r.reason and new_r.reason not in (cur.reason or ""):
                    combined_reason = f"{cur.reason}; {new_r.reason}" if cur.reason else new_r.reason
                rule_map[new_r.pattern] = ReplacementRule(
                    pattern=cur.pattern,
                    replacement=cur.replacement,
                    reason=combined_reason,
                    hit_count=cur.hit_count,
                    last_hit_at=cur.last_hit_at,
                    added_at=cur.added_at,
                    source_video_id=cur.source_video_id or new_r.source_video_id,
                    is_archived=cur.is_archived,
                    tags=combined_tags,
                )
            else:
                rule_map[new_r.pattern] = new_r
        self.replacements = list(rule_map.values())

        hints = []
        if self.second_pass_hints:
            hints.append(self.second_pass_hints.strip())
        if other.second_pass_hints and other.second_pass_hints.strip() not in hints:
            hints.append(other.second_pass_hints.strip())
        self.second_pass_hints = "\n".join(hints) if hints else None

        # Merge voiceprint embeddings (weighted centroid average + L2 normalization)
        if other.voiceprint:
            if not self.voiceprint:
                self.voiceprint = list(other.voiceprint)
                self.voiceprint_sample_count = other.voiceprint_sample_count or 1
                self.voiceprint_threshold = other.voiceprint_threshold or 0.45
            else:
                n1 = max(1, self.voiceprint_sample_count)
                n2 = max(1, other.voiceprint_sample_count)
                total_n = n1 + n2
                vec1 = np.array(self.voiceprint, dtype=np.float32)
                vec2 = np.array(other.voiceprint, dtype=np.float32)
                merged_vec = (vec1 * n1 + vec2 * n2) / total_n
                norm = float(np.linalg.norm(merged_vec))
                if norm > 1e-9:
                    merged_vec = merged_vec / norm
                self.voiceprint = merged_vec.tolist()
                self.voiceprint_sample_count = total_n

        if other.lora_adapter_path:
            self.lora_adapter_path = other.lora_adapter_path
            self.lora_trained_video_count = max(self.lora_trained_video_count + 1, other.lora_trained_video_count)

        self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self

    @field_validator("speaker_id")
    @classmethod
    def sanitize_speaker_id(cls, v: str) -> str:
        s = v.strip().lower()
        # Only allow alphanumeric, hyphen, underscore
        sanitized = re.sub(r"[^a-z0-9_-]", "_", s)
        sanitized = re.sub(r"_+", "_", sanitized).strip("_")
        if not sanitized:
            sanitized = "speaker_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return sanitized

    def to_replace_text(self) -> str:
        """Convert active replacements to hayamimi standard --replace compatible text.
        Format:
        # Speaker Profile: <display_name> (<speaker_id>)
        pattern\treplacement
        """
        lines = [
            f"# Speaker Profile: {self.display_name} ({self.speaker_id})",
            f"# Generated at: {self.created_at}",
            "",
        ]
        for rule in self.replacements:
            # Exclude archived rules from live replace text
            if rule.is_archived:
                continue
            if rule.pattern and rule.replacement:
                # Tab-separated
                lines.append(f"{rule.pattern}\t{rule.replacement}")
        return "\n".join(lines) + "\n"

    def filter_safe_replacements(self, min_len: int = 2) -> List[ReplacementRule]:
        """Filter out potentially dangerous short replacements (e.g. 1 char words like 'と' -> 'そ')."""
        safe_rules = []
        for r in self.replacements:
            if r.is_archived:
                continue
            p = r.pattern.strip()
            # If pattern is too short or is a common punctuation/particle, skip
            if len(p) < min_len:
                continue
            safe_rules.append(r)
        return safe_rules

