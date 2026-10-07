"""Console and rotating UTF-8 logs containing operational metadata only."""
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SafeFilter(logging.Filter):
    def __init__(self, configs):
        super().__init__()
        self.configs = configs

    def filter(self, record):
        message = record.getMessage()
        for config in self.configs.values():
            if config and config.api_key:
                message = message.replace(config.api_key, "[REDACTED]")
        message = re.sub(r"(?i)(authorization|api[_-]?key|bearer)\s*[:=]?\s*\S+", r"\1=[REDACTED]", message)
        record.msg, record.args = message, ()
        # Exception bodies are not safe operational metadata.
        record.exc_info, record.exc_text = None, None
        return True


def configure_logging(data_dir, configs, *, max_bytes=2_000_000, backups=3):
    directory = Path(data_dir) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("iris")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in (logging.StreamHandler(), RotatingFileHandler(directory / "iris.log", maxBytes=max_bytes,
                                                                  backupCount=backups, encoding="utf-8")):
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.addFilter(SafeFilter(configs))
        logger.addHandler(handler)
    return logger
