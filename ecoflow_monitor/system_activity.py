from dataclasses import dataclass, replace


class ActivityCancelled(Exception):
    pass


@dataclass(frozen=True)
class SystemActivity:
    sleeping: bool = False
    locked: bool = False
    display_off: bool = False
    awaiting_user: bool = False

    @property
    def paused(self) -> bool:
        return self.sleeping or self.locked or self.display_off or self.awaiting_user

    def transition(self, event: str):
        if event == "suspend":
            return replace(self, sleeping=True, awaiting_user=True)
        if event == "resume":
            return replace(self, sleeping=False)
        if event == "resume_user":
            return replace(self, sleeping=False, awaiting_user=False)
        if event == "lock":
            return replace(self, locked=True)
        if event == "unlock":
            return replace(self, locked=False, awaiting_user=self.awaiting_user and self.sleeping)
        if event == "display_off":
            return replace(self, display_off=True)
        if event in {"display_on", "display_dim"}:
            return replace(self, display_off=False, awaiting_user=self.awaiting_user and self.sleeping)
        return self
