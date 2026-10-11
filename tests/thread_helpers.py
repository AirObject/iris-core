"""Deterministic ordering for tests of real model-worker timeout handling."""

# A deadlock guard, not a model deadline or a required scheduling latency.
THREAD_WAIT = 10


def expire_after_entry(monkeypatch, pool, entered, clock, *, expire=None):
    """Expire a real running Future only after its blocked handler has entered.

    Keep submit/cancel/done callbacks real so admission release is still tested.
    Only the caller's first result wait and its clock are controlled.
    """
    submit = pool.submit
    requests = []

    def controlled_submit(*args, **kwargs):
        request = submit(*args, **kwargs)
        requests.append(request)
        result = request.result

        def deadline_result(timeout=None):
            request.result = result
            assert timeout is not None and timeout > 0
            assert entered.wait(THREAD_WAIT), "model worker did not enter"
            if expire is not None:
                assert expire.wait(THREAD_WAIT), "test did not release the deadline"
            assert request.running() and not request.done()
            clock.advance(timeout)
            # The handler is still blocked: the real Future raises TimeoutError.
            return result(timeout=0)

        monkeypatch.setattr(request, 'result', deadline_result)
        return request

    monkeypatch.setattr(pool, 'submit', controlled_submit)
    return requests
