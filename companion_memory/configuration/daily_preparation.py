"""Prepare an explicit timezone from trusted process settings before pure parsing.

An operator value wins. Otherwise TZ, a zoneinfo localtime link, or the system
timezone file must identify an installed IANA zone. Ambiguous offsets and invalid
environment settings require explicit configuration; no geographic guess is made.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .daily_resolution import configuration_failure, resolve_daily_configuration


def _valid_zone(value: object) -> bool:
    if type(value) is not str or not value or len(value.encode()) > 128 or value.startswith(('/', ':')):
        return False
    try:
        ZoneInfo(value)
        return True
    except (ValueError, ZoneInfoNotFoundError):
        return False


def _process_zone() -> str | None:
    if 'TZ' in os.environ:
        value = os.environ['TZ']
        return value if _valid_zone(value) else None
    try:
        localtime = Path('/etc/localtime')
        if localtime.is_symlink():
            target = str(localtime.resolve(strict=True))
            if '/zoneinfo/' in target:
                value = target.split('/zoneinfo/', 1)[1]
                if _valid_zone(value):
                    return value
        timezone = Path('/etc/timezone')
        if timezone.is_file() and timezone.stat().st_size <= 256:
            value = timezone.read_text(encoding='utf-8').strip()
            if _valid_zone(value):
                return value
    except (OSError, UnicodeError, RuntimeError):
        return None
    return None


def environment_timezone() -> str | None:
    """Return a trusted unambiguous installed zone for a new management form.

    An abbreviation such as EST is not interpreted as a geographic region.
    Existing explicit configuration and the input timestamp protocol are untouched.
    A missing result requires the operator to enter and confirm a valid zone.
    """
    zone = _process_zone()
    return zone if zone is not None and (zone == 'UTC' or '/' in zone) else None


def prepare_daily_configuration(foundation: object, runtime: object, platforms: object,
                                content: object, information: object, daily: object,
                                protected_directories: object, material_contracts: object):
    """Resolve a complete new candidate without changing caller data or persisted values.

    OPEN_EXISTING callers reconstruct their original explicit candidate and use
    the persistent loader. This preparation entry grants no activation authority.
    """
    if type(daily) is not dict or type(daily.get('explicit_values')) is not dict:
        return configuration_failure('INVALID_INPUT', 'runtime.timezone', 'INVALID_SHAPE', 'prepare_daily_configuration')
    values = dict(daily['explicit_values'])
    if 'runtime.timezone' not in values:
        zone = _process_zone()
        if zone is None:
            return configuration_failure('INVALID_INPUT', 'runtime.timezone', 'EXPLICIT_CONFIGURATION_REQUIRED', 'prepare_daily_configuration')
        values['runtime.timezone'] = zone
    return resolve_daily_configuration(foundation, runtime, platforms, content, information,
        {**daily, 'explicit_values': values}, protected_directories, material_contracts)
