def normalize_secret_reference(reference: str) -> str:
    reference = reference.strip()
    if reference.startswith(('"', "'")):
        if len(reference) < 2 or reference[-1] != reference[0]:
            raise ValueError("onepassword_reference_error")
        reference = reference[1:-1]
    elif reference.endswith(('"', "'")):
        raise ValueError("onepassword_reference_error")
    parts = reference.removeprefix("op://").split("/")
    if (not reference.startswith("op://") or len(parts) not in (3, 4)
            or any(not part.strip() for part in parts)
            or any(ord(character) < 32 or ord(character) == 127 for character in reference)
            or "?" in reference or "#" in reference):
        raise ValueError("onepassword_reference_error")
    return reference
