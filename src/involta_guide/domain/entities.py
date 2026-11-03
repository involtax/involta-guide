"""Business errors shared by application boundaries."""


class RuleViolation(ValueError):
    pass


class Conflict(RuleViolation):
    pass


class NotFound(RuleViolation):
    pass
