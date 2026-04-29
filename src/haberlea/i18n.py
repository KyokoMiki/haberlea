"""Internationalization (i18n) support for Haberlea using Babel."""

import gettext
import logging
import os
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

logger = logging.getLogger(__name__)

# Supported languages
SUPPORTED_LANGUAGES = ["zh_CN", "en_US"]
DEFAULT_LANGUAGE = "zh_CN"

# Source language — msgids are written in this language, so no translation
# catalog is needed. `_()` returns the msgid as-is for this language.
SOURCE_LANGUAGE = "en_US"

# Translations directory
LOCALE_DIR = Path(__file__).parent / "locales"

# Current translation objects
_translations: dict[str, gettext.GNUTranslations | gettext.NullTranslations] = {}
_current_language = DEFAULT_LANGUAGE


def _compile_po_to_mo(po_path: Path, mo_path: Path) -> bool:
    """Compile .po file to .mo file.

    Args:
        po_path: Path to .po source file.
        mo_path: Path to .mo target file.

    Returns:
        True if compilation succeeded, False otherwise.
    """
    try:
        with open(po_path, "rb") as po_file:
            catalog = read_po(po_file)

        mo_path.parent.mkdir(parents=True, exist_ok=True)
        with open(mo_path, "wb") as mo_file:
            write_mo(mo_file, catalog)

        logger.debug(f"Compiled {po_path} -> {mo_path}")
        return True
    except ImportError:
        logger.warning("babel not installed, cannot compile .po files")
        return False
    except Exception as e:
        logger.warning(f"Failed to compile {po_path}: {e}")
        return False


def _ensure_mo_files() -> None:
    """Ensure .mo files exist and are up-to-date.

    Automatically compiles .po files to .mo if:
    - .mo file doesn't exist
    - .mo file is older than .po file
    """
    for lang in SUPPORTED_LANGUAGES:
        # Skip source language — msgids are already in this language.
        if lang == SOURCE_LANGUAGE:
            continue

        po_path = LOCALE_DIR / lang / "LC_MESSAGES" / "messages.po"
        mo_path = LOCALE_DIR / lang / "LC_MESSAGES" / "messages.mo"

        if not po_path.exists():
            continue

        # Check if .mo needs to be (re)compiled
        needs_compile = False
        if not mo_path.exists():
            needs_compile = True
            logger.debug(f"Missing .mo file for {lang}, will compile")
        elif po_path.stat().st_mtime > mo_path.stat().st_mtime:
            needs_compile = True
            logger.debug(f".po file newer than .mo for {lang}, will recompile")

        if needs_compile:
            _compile_po_to_mo(po_path, mo_path)


def init_i18n() -> None:
    """Initialize i18n system and load all translations."""
    global _translations

    # Ensure locale directory exists
    LOCALE_DIR.mkdir(exist_ok=True)

    # Auto-compile .po to .mo if needed
    _ensure_mo_files()

    # Load translations for all supported languages
    for lang in SUPPORTED_LANGUAGES:
        # Source language: msgids are the source strings — use a no-op
        # translator that returns msgid unchanged.
        if lang == SOURCE_LANGUAGE:
            _translations[lang] = gettext.NullTranslations()
            logger.debug(f"Using source language as-is for {lang}")
            continue

        mo_path = LOCALE_DIR / lang / "LC_MESSAGES" / "messages.mo"
        if not mo_path.exists():
            logger.warning(
                f"Missing translation catalog for {lang} at {mo_path}, "
                "messages will fall back to source language"
            )
            _translations[lang] = gettext.NullTranslations()
            continue

        try:
            trans = gettext.translation(
                "messages",
                localedir=str(LOCALE_DIR),
                languages=[lang],
            )
            _translations[lang] = trans
            logger.debug(f"Loaded translations for {lang}")
        except Exception as e:
            logger.warning(f"Failed to load translations for {lang}: {e}")
            _translations[lang] = gettext.NullTranslations()

    # Set default language from environment or settings
    env_lang = os.getenv("HABERLEA_LANG", DEFAULT_LANGUAGE)
    set_language(env_lang)

    logger.info(f"i18n initialized with locale: {_current_language}")


def set_language(language: str) -> None:
    """Set the current language.

    Args:
        language: Language code (e.g., 'zh_CN', 'en_US').
    """
    global _current_language

    if language not in SUPPORTED_LANGUAGES:
        logger.warning(f"Unsupported language: {language}, using default")
        language = DEFAULT_LANGUAGE

    _current_language = language
    logger.debug(f"Language set to: {language}")


def get_current_language() -> str:
    """Get the current language.

    Returns:
        Current language code.
    """
    return _current_language


def _(message: str) -> str:
    """Translate a message to the current language.

    This is the standard gettext function name for translations.

    Args:
        message: Message to translate (English source text).

    Returns:
        Translated string.
    """
    trans = _translations.get(_current_language)
    if trans is None:
        return message
    return trans.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Translate a message with plural forms.

    Args:
        singular: Singular form message.
        plural: Plural form message.
        n: Number to determine which form to use.

    Returns:
        Translated string in appropriate form.
    """
    trans = _translations.get(_current_language)
    if trans is None:
        return singular if n == 1 else plural
    return trans.ngettext(singular, plural, n)


# Initialize on module import
init_i18n()
