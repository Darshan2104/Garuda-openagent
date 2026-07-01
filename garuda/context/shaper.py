def shape_observation(text: str, max_bytes: int) -> str:
    if not text:
        return "Command ran successfully with no output."
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    half = max_bytes // 2
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-half:].decode("utf-8", errors="ignore")
    omitted = len(encoded) - max_bytes
    return f"{head}\n\n...[truncated {omitted} bytes]...\n\n{tail}"
