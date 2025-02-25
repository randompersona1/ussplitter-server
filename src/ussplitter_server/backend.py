"""
Backend for the USSplitter server. This module contains functions to interact with
the database and to separate audio files.
"""

import contextlib
import logging
import os
import shlex
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from enum import Enum, auto, unique
from pathlib import Path
from typing import Generator, cast

import demucs.api
import demucs.separate
import platformdirs
import torch as th

FILE_DIRECTORY = Path(
    platformdirs.user_data_dir(
        "ussplitter", roaming=False, appauthor=False, ensure_exists=True
    )
)
DB_PATH = FILE_DIRECTORY.joinpath("db.sqlite")
DEFAULT_MODEL = "htdemucs"


logging.basicConfig(
    format="%(asctime)s,%(msecs)03d %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG,
)
logger = logging.getLogger(__name__)


@unique
class SplitStatus(Enum):
    """Status of a song in the queue"""

    NONE = auto()
    PENDING = auto()
    PROCESSING = auto()
    FINISHED = auto()
    ERROR = auto()


@dataclass(frozen=True)
class SplitArgs:
    """Arguments for the audio splitter"""

    input_file: Path
    output_dir: Path
    bitrate: int = 128
    model: str = DEFAULT_MODEL


class AudioSplitError(Exception):
    """Base class for exceptions in this module"""

    def __init__(self, message: str):
        self.message = message

    def __str__(self):
        return self.message


class ArgsError(AudioSplitError):
    """Exception raised for errors in the arguments"""


def init_db() -> None:
    """Initialize the database"""
    logger.debug("Initializing database.")
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS queue (
                song_uuid TEXT PRIMARY KEY NOT NULL,
                model TEXT
            )
        """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS status (
                song_uuid TEXT PRIMARY KEY NOT NULL,
                status TEXT
            )
        """
        )


@contextlib.contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Get a connection to the database"""
    try:
        conn = sqlite3.connect(DB_PATH)
    except sqlite3.Error as e:
        logger.error("Error connecting to database: %s", e)
        raise e
    try:
        yield conn
    finally:
        conn.close()


def get_models() -> list[str]:
    """
    Get a list of available models

    :return: A list of available models
    """
    logger.debug("Getting available models.")
    models = []
    for model in demucs.api.list_models().get("single"):
        models.append(model)
    for model in demucs.api.list_models().get("bag"):
        models.append(model)
    return models


def make_folder() -> tuple[str, Path]:
    """
    Create a new directory for a song to be stored in

    :return: A tuple containing the UUID of the song and the path where the mp3 file will be stored
    """

    song_uuid = str(uuid.uuid4())
    logger.debug("Creating directory for %s.", song_uuid)

    tempdir = FILE_DIRECTORY.joinpath(song_uuid)
    tempdir.mkdir(exist_ok=False)

    mp3_file = tempdir.joinpath("input.mp3")

    return song_uuid, mp3_file


def put(song_uuid: str, model: str | None = None) -> None:
    """
    Put a song into the queue to be separated

    :param uuid: The UUID of the song
    :return: None
    """
    logger.debug("Got %s with model %s. Queuing.", song_uuid, model)

    with get_db() as db:
        db.execute(
            "INSERT INTO queue (song_uuid, model) VALUES (?, ?)", (song_uuid, model)
        )
        db.execute(
            "INSERT INTO status (song_uuid, status) VALUES (?, ?)",
            (song_uuid, SplitStatus.PENDING.name),
        )
        db.commit()


def get_status(song_uuid: str) -> SplitStatus:
    """
    Get the status of a song

    :param song_uuid: The UUID of the song
    :return: The status of the song
    """

    with get_db() as db:
        status = db.execute(
            "SELECT status FROM status WHERE song_uuid = ?", (song_uuid,)
        )
        result = status.fetchone()
        if result is None:
            return SplitStatus.NONE
        logger.debug("Got status %s for %s.", result[0], song_uuid)
        return SplitStatus[result[0]]


def get_vocals(song_uuid: str) -> Path:
    """
    Get the path to the vocals file. This path is not guaranteed to exist. Only try to access
    the file if the status is FINISHED

    :param song_uuid: The UUID of the song
    :return: The path to the vocals file
    """
    logger.debug("Getting vocals for %s.", song_uuid)

    for path in FILE_DIRECTORY.joinpath(song_uuid).rglob("vocals.mp3"):
        return path

    raise FileNotFoundError(f"Vocals file not found for {song_uuid}.")


def get_instrumental(song_uuid: str) -> Path:
    """
    Get the path to the instrumental file. This path is not guaranteed to exist. Only try to access
    this file if the status is FINISHED.

    :param song_uuid: The UUID of the song
    :return: The path to the instrumental file
    """
    logger.debug("Getting instrumental for %s.", song_uuid)

    # We need the model name to get the correct file
    for path in FILE_DIRECTORY.joinpath(song_uuid).rglob("no_vocals.mp3"):
        return path

    raise FileNotFoundError(f"Instrumental file not found for {song_uuid}.")


def cleanup(song_uuid: str) -> bool:
    """
    Remove the files associated with a song

    :param song_uuid: The UUID of the song
    :return: None
    """
    logger.debug("Cleaning up %s.", song_uuid)

    with get_db() as db:
        status = db.execute(
            "SELECT status FROM status WHERE song_uuid = ?", (song_uuid,)
        )
        status = status.fetchone()
        if (
            status is None
            or cast(str, status[0]) == SplitStatus.NONE.name  # type: ignore
            or cast(str, status[0]) == SplitStatus.PROCESSING.name  # type: ignore
            or cast(str, status[0]) == SplitStatus.PENDING.name  # type: ignore
        ):
            logger.debug("Song %s is not finished. Not cleaning up.", song_uuid)
            return False

    path = FILE_DIRECTORY.joinpath(song_uuid)
    shutil.rmtree(path)

    with get_db() as db:
        db.execute("DELETE FROM queue WHERE song_uuid = ?", (song_uuid,))
        db.execute("DELETE FROM status WHERE song_uuid = ?", (song_uuid,))
        db.commit()

    return True


def cleanup_all() -> bool:
    """
    Remove all files associated with all songs. Only allowed if there are no songs currently being
    processed

    :return: None
    """
    # Check if there are any songs with PROCESSING or PENDING status. If there are, return False
    with get_db() as db:
        status = db.execute("SELECT status FROM status")
        songs = status.fetchall()
        for song in songs:
            if (
                song[0] == SplitStatus.PROCESSING.name
                or song[0] == SplitStatus.PENDING.name
            ):
                return False

    # If there are no songs with PROCESSING or PENDING status, remove all files

    for songfolder in FILE_DIRECTORY.iterdir():
        shutil.rmtree(songfolder)

    with get_db() as db:
        db.execute("DELETE FROM queue")
        db.execute("DELETE FROM status")
        db.commit()

    return True


def split_worker() -> None:
    """Entrypoint for the split worker. Make sure this is running only once. The worker will check the
    queue for songs to separate and separate them."""

    # Debug information
    logger.debug("Starting split worker.")
    logger.debug("File directory: %s", FILE_DIRECTORY)
    logger.debug("Database path: %s", {DB_PATH})
    logger.debug("GPU available: %s", str(th.cuda.is_available()))
    logger.debug("Available models: %s", demucs.api.list_models())

    init_db()

    while True:
        # Get a song to separate. If not available, sleep for 1 second and try again
        with get_db() as db:
            task = db.execute("SELECT * FROM queue LIMIT 1")
            task = task.fetchone()

        if task is None:
            time.sleep(1)
            continue

        song_uuid = cast(str, task[0])  # type: ignore
        model = cast(str, task[1])  # type: ignore
        if model is None or model == "":
            logger.info(
                "No model specified for %s. Using default model %s.",
                song_uuid,
                DEFAULT_MODEL,
            )
            model = DEFAULT_MODEL
        elif (
            demucs.api.list_models().get("single") is None
            or model not in demucs.api.list_models().get("single").keys()  # type: ignore
        ) and (
            demucs.api.list_models().get("bag") is None
            or model not in demucs.api.list_models().get("bag").keys()  # type: ignore
        ):
            logger.warning(
                'Invalid model "%s". Using default model %s.', model, DEFAULT_MODEL
            )
            model = DEFAULT_MODEL
        elif model.endswith("_q"):
            logger.warning(
                "Model %s is quantized. Quantized models are not supported. Using default model "
                "%s.",
                model,
                DEFAULT_MODEL,
            )
            model = DEFAULT_MODEL

        logger.info("Picked up task %s with model %s.", song_uuid, model)

        with get_db() as db:
            db.execute("DELETE FROM queue WHERE song_uuid = ?", (song_uuid,))
            db.execute(
                "UPDATE status SET status = ? WHERE song_uuid = ?",
                (SplitStatus.PROCESSING.name, song_uuid),
            )
            db.commit()

        path = FILE_DIRECTORY.joinpath(song_uuid)
        input_file = path.joinpath("input.mp3")

        args = SplitArgs(input_file=input_file, output_dir=path, model=model)

        timebefore = time.time()

        try:
            separate_audio(args)
            with get_db() as db:
                db.execute(
                    "UPDATE status SET status = ? WHERE song_uuid = ?",
                    (SplitStatus.FINISHED.name, song_uuid),
                )
                db.commit()
            logger.info("Done. Separation took %.2f seconds.", time.time() - timebefore)
        except AssertionError as e:
            with get_db() as db:
                db.execute(
                    "UPDATE status SET status = ? WHERE song_uuid = ?",
                    (SplitStatus.ERROR.name, song_uuid),
                )
                db.commit()
            raise AudioSplitError(e) from e
        except ArgsError as e:
            with get_db() as db:
                db.execute(
                    "UPDATE status SET status = ? WHERE song_uuid = ?",
                    (SplitStatus.ERROR.name, song_uuid),
                )
                db.commit()
            raise e


def separate_audio(args: SplitArgs) -> None:
    """
    :param args: SplitArgs object
    :return: None

    :raises AssertionError: If the input file does not exist or is not a file, or if the output directory does not exist or is not a directory
    :raises ArgsError: If the arguments are invalid
    """
    assert args.input_file.exists(), args.input_file.is_file()
    assert args.output_dir.exists(), args.output_dir.is_dir()
    assert args.bitrate > 0, args.bitrate < 320

    try:
        demucs_args = shlex.split(
            f'--mp3 --mp3-bitrate={str(args.bitrate)} --two-stems=vocals -n {args.model} -j 2 -o "{args.output_dir.as_posix()}" "{args.input_file.as_posix()}"'
        )
    except ValueError as e:
        raise ArgsError(e) from e
    with contextlib.ExitStack() as stack:
        # Redirect stdout and stderr to null
        null_file = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
        stack.enter_context(contextlib.redirect_stdout(null_file))
        stack.enter_context(contextlib.redirect_stderr(null_file))

        # Temporarily disable tqdm progress bars
        stack.enter_context(contextlib.suppress(Exception))
        if "tqdm" in sys.modules:
            sys.modules["tqdm"].tqdm = lambda *args, **kwargs: args[0] if args else None  # type: ignore
        logger.debug("Running `demucs %s`.", " ".join(demucs_args))
        demucs.separate.main(demucs_args)
