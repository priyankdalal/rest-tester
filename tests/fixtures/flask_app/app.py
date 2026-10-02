"""Flask fixture used by the scanner tests."""

from flask import Blueprint, Flask

app = Flask(__name__)
trials = Blueprint("trials", __name__, url_prefix="/trials")


class TrialResponse:
    id: int
    name: str


@app.route("/ping")
def ping():
    return "pong"


@trials.route("/<int:trial_id>", methods=["GET", "DELETE"])
def trial_detail(trial_id) -> TrialResponse:
    return {"id": trial_id}


@trials.route("/", methods=["POST"])
def create_trial():
    return {}


app.register_blueprint(trials)
