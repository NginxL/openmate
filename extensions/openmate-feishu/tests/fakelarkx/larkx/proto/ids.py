import itertools

_n = itertools.count(1)


def generate_long_request_id():
    return "req-%d" % next(_n)
