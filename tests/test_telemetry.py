from services import telemetry
from services.translation_engines import config


class Instrument:
    def __init__(self):
        self.samples = []

    def add(self, value, attributes):
        self.samples.append((value, attributes))

    def record(self, value, attributes):
        self.samples.append((value, attributes))


def test_translation_metrics_include_engine_dimensions_without_document_text(monkeypatch):
    instruments = {name: Instrument() for name in ("tokens", "characters", "cost", "duration")}
    monkeypatch.setattr(telemetry, "_instruments", lambda: instruments)
    assert telemetry.record_translation_metrics(
        "job-1", config("gpt6_luna"),
        {"inputTokens": 10, "outputTokens": 4, "reasoningTokens": 1,
         "charactersCharged": None},
        duration_by_step={"translate": 1.5}, estimated_cost=0.25,
    )
    dimensions = instruments["tokens"].samples[0][1]
    assert dimensions["jobId"] == "job-1"
    assert dimensions["engine"] == "gpt6_luna"
    assert dimensions["reasoningEffort"] == "low"
    assert not any("text" in key.lower() or "excerpt" in key.lower() for key in dimensions)
    assert [sample[0] for sample in instruments["tokens"].samples] == [10, 4, 1]
    assert instruments["cost"].samples == [(
        0.25, {key: value for key, value in dimensions.items() if key != "tokenType"},
    )]
    assert instruments["duration"].samples[0][0] == 1.5


def test_metrics_are_not_emitted_when_application_insights_is_unconfigured(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    telemetry._instruments.cache_clear()
    assert telemetry.record_translation_metrics(
        "job-2", config("translator"), {}, duration_by_step={}, estimated_cost=None,
    ) is False
    telemetry._instruments.cache_clear()
