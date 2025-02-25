import threading

import flask

from ussplitter_server import backend

app = flask.Flask(__name__)


@app.route("/split", methods=["POST"])
def split():
    # Create a temporary directory for everything to be stored in
    input_file = flask.request.files.get("audio")
    if input_file is None:
        return "No audio file provided", 400

    # get model from request args
    model = flask.request.args.get(key="model", type=str)

    song_uuid, song_path = backend.make_folder()
    input_file.save(song_path)

    backend.put(song_uuid, model)

    # Return uuid so the files can be retrieved
    return song_uuid, 200


@app.route("/result/vocals", methods=["GET"])
def get_vocals():
    uuid = flask.request.args.get(key="uuid", type=str)
    if not uuid:
        return "No uuid provided", 400

    vocals_path = backend.get_vocals(uuid)
    return flask.send_file(vocals_path)


@app.route("/result/instrumental", methods=["GET"])
def get_instrumental():
    uuid = flask.request.args.get("uuid")
    if not uuid:
        return "No uuid provided", 400

    instrumental_path = backend.get_instrumental(uuid)
    return flask.send_file(instrumental_path)


@app.route("/status", methods=["GET"])
def get_status():
    uuid = flask.request.args.get("uuid")
    if not uuid:
        return "No uuid provided", 400

    status = backend.get_status(uuid)
    return status.name, 200


@app.route("/cleanup", methods=["POST"])
def cleanup():
    uuid = flask.request.args.get("uuid")
    if not uuid:
        return "No uuid provided", 400

    success = backend.cleanup(uuid)
    if success:
        return "Success", 200
    else:
        return "Failed", 500


@app.route("/cleanupall", methods=["POST"])
def cleanup_all():
    success = backend.cleanup_all()

    if success:
        return "Success", 200
    else:
        return "Failed", 500


# Start the split worker in a separate thread
split_thread = threading.Thread(target=backend.split_worker)
split_thread.start()
