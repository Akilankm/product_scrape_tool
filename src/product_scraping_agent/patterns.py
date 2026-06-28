"""Regex patterns used only by the scraping runtime."""

from __future__ import annotations

import re

NON_DIGITS = re.compile(r"\D")

CAPTCHA_SIGNALS = re.compile(
    r"(?i)(captcha|robot check|verify you are human|are you a robot|"
    r"automated access|bot detection|access denied|please enable javascript|"
    r"unusual traffic|challenge-platform|cf-turnstile|hcaptcha|recaptcha)"
)

MD_IMAGE = re.compile(r'!\[[^\]]*\]\([^)]+\)')
MD_LINK = re.compile(r'\[([^\]]*)\]\([^)]+\)')
EXCESS_NEWLINES = re.compile(r'\n{3,}')
EXCESS_SPACES = re.compile(r'[ \t]{3,}')
NOISE_BOUNDARY = re.compile(
    r"(?i)(similar products|related products|customers also|"
    r"you may also|recommended for you|recently viewed|footer|"
    r"copyright|newsletter|cookie|privacy policy|sign up|subscribe)"
)

IMAGE_SKIP_DOWNLOAD = re.compile(
    r"(sprite|favicon|logo|placeholder|pixel|tracking|social[-_]?icon|"
    r"payment[-_]?icon|loader|spinner)",
    re.IGNORECASE,
)
