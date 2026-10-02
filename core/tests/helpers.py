"""Small assertion helpers shared by the tests."""


def only(exceptions):
    assert len(exceptions) == 1, exceptions
    return exceptions[0]


def steps(line, label_prefix):
    return [s for s in line.trail if s.label.startswith(label_prefix)]
