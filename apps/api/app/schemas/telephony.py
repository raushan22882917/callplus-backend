from pydantic import BaseModel


class TwilioStatusCallback(BaseModel):
    CallSid: str
    CallStatus: str
    Duration: str | None = None
    RecordingUrl: str | None = None
