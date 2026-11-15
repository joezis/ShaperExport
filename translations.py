"""Load ShaperExport's UI strings from the selected language file."""
import json
import os


def _language_code():
    languages = []
    try:
        import FreeCAD as App
        configured = App.ParamGet("User parameter:BaseApp/Preferences/General").GetString(
            "Language", "")
        if configured:
            languages.append(configured)
        locale = App.getLocale()
        if locale:
            languages.append(locale)
    except Exception:
        pass
    try:
        try:
            from PySide import QtCore
        except ImportError:
            try:
                from PySide2 import QtCore
            except ImportError:
                from PySide6 import QtCore
        languages.append(QtCore.QLocale().name())
    except Exception:
        QtCore = None

    for language in languages:
        raw = str(language).strip()
        if not raw:
            continue
        if QtCore is not None:
            try:
                locale = QtCore.QLocale(raw)
                locale_name = str(locale.name())
                if locale_name.upper() != "C":
                    return locale_name.split("_", 1)[0].split("-", 1)[0].lower()
            except Exception:
                pass
            # Some FreeCAD versions expose the preference as a translated
            # language name rather than a locale code. Resolve that name
            # through Qt's language enum instead of maintaining a language map.
            try:
                enum_types = [getattr(QtCore.QLocale, "Language", QtCore.QLocale),
                              QtCore.QLocale]
                for enum_type in enum_types:
                    for attribute in dir(enum_type):
                        try:
                            value = getattr(enum_type, attribute)
                            name = QtCore.QLocale.languageToString(value)
                            if str(name).casefold() == raw.casefold():
                                return str(QtCore.QLocale(value).name()).split("_", 1)[0].lower()
                        except Exception:
                            continue
            except Exception:
                pass
        # Accept ISO-style codes even when Qt cannot identify the locale.
        code = raw.split("_", 1)[0].split("-", 1)[0].lower()
        if code.isalpha() and 2 <= len(code) <= 3:
            return code
        return code
    return "en"


LANGUAGE = _language_code()
_directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")
_language_file = os.path.join(_directory, LANGUAGE + ".json")
if not os.path.isfile(_language_file):
    _language_file = os.path.join(_directory, "en.json")
with open(_language_file, encoding="utf-8") as _source:
    _STRINGS = json.load(_source)


def translate(key):
    """Return a translated string, keeping unknown keys visible for debugging."""
    return _STRINGS.get(key, key)
