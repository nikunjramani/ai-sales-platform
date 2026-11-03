"""Background worker process: claims durable jobs and runs their handlers.

Run: uv run python -m sales_platform.worker            (until Ctrl+C)
     uv run python -m sales_platform.worker --once     (drain available jobs, then exit)
Several workers can run at once; the queue gives each job to one worker at a time.
Handlers are wired in sales_platform.composition, shared with the tests.
"""

import argparse
import logging
import time

from sales_platform.composition import build_job_handlers, build_runtime
from sales_platform.enquiries.adapters.interpreters.factory import build_interpreter
from sales_platform.enquiries.persistence.workflow import open_checkpointer
from sales_platform.platform.application.jobs import JobRunner
from sales_platform.platform.config import Settings
from sales_platform.platform.persistence.database import build_engine
from sales_platform.platform.persistence.jobs import SqlJobQueue
from sales_platform.quotations.adapters.interpreters.reply_classifier import build_reply_classifier


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="process available jobs and exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = Settings()
    engine = build_engine(settings.worker_url())
    api_key = settings.model_api_key.get_secret_value() if settings.model_api_key else None
    interpreter = build_interpreter(settings.model, api_key, settings.model_options)
    reply_classifier = build_reply_classifier(settings.model, api_key, settings.model_options)
    runtime = build_runtime(settings, engine)
    logging.info("Enquiry interpreter: %s", settings.model or "rules (no AI)")
    logging.info("Providers: %s", ", ".join(p.key for p in runtime.registry.all()) or "none")
    try:
        with open_checkpointer(settings.worker_url()) as checkpointer:
            runner = JobRunner(
                SqlJobQueue(engine),
                build_job_handlers(
                    settings,
                    engine,
                    runtime,
                    checkpointer,
                    interpreter,
                    reply_classifier=reply_classifier,
                ),
            )
            while True:
                while runner.run_once():
                    pass
                if arguments.once:
                    return
                time.sleep(arguments.poll_seconds)
    except KeyboardInterrupt:
        logging.info("Worker stopped")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
