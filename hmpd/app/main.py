"""Entrypoint: set up logging, load options, run the bridge until shutdown."""
import logging
import logging.handlers
import os

from hmpd_bridge.bridge import HMPDBridge
from hmpd_bridge.config import DEBUG_LOG_BACKUP_COUNT, DEBUG_LOG_FILE, DEBUG_LOG_MAX_BYTES, load_options


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not debug:
        return

    try:
        os.makedirs(os.path.dirname(DEBUG_LOG_FILE), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            DEBUG_LOG_FILE,
            maxBytes=DEBUG_LOG_MAX_BYTES,
            backupCount=DEBUG_LOG_BACKUP_COUNT,
        )
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger("hmpd_bridge").info(
            "Debug logging to %s with rotation (max %dMB, %d backups)",
            DEBUG_LOG_FILE,
            DEBUG_LOG_MAX_BYTES // (1024 * 1024),
            DEBUG_LOG_BACKUP_COUNT,
        )
    except OSError as exc:
        logging.getLogger("hmpd_bridge").warning("Could not set up debug file handler: %s", exc)


def main() -> None:
    options = load_options()
    configure_logging(options.debug)
    log = logging.getLogger("hmpd_bridge")

    try:
        HMPDBridge(options).start()
    except KeyboardInterrupt:
        log.info("Received SIGINT, shutting down gracefully")
    except Exception:
        log.critical("Fatal error", exc_info=True)
        raise


if __name__ == "__main__":
    main()
