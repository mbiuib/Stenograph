"""Shared domain exceptions."""


class JobCancelled(Exception):
    """Raised inside a worker to stop cooperatively.

    Engines check the cancellation callback between segments and raise this;
    the pipeline translates it into the "cancelled" terminal state.
    """
