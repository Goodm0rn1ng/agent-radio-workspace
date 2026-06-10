"""Shared transcript normalization for known ASR domain-term errors."""
from __future__ import annotations


# Order matters: longer/compound forms first, so a later short rule does not
# corrupt an already-fixed string. Only the agency compound forms are corrected;
# bare surnames like 青木陽菜 (a real seiyuu) must be left untouched.
ASR_CORRECTIONS = {
    "青鬼プロナクション": "青二プロダクション",
    "青木プロナクション": "青二プロダクション",
    "青鬼プロダクション": "青二プロダクション",
    "青木プロダクション": "青二プロダクション",
    "青鬼プロ": "青二プロ",
    "青木プロ": "青二プロ",
}


def normalize_transcript_text(text: str) -> str:
    for wrong, correct in ASR_CORRECTIONS.items():
        text = text.replace(wrong, correct)
    return text
