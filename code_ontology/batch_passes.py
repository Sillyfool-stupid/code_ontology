"""Bounded stage passes: retry unfinished batches without repeating successes."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time

from .events import emit
from .llm import BudgetError, NonRetryableModelFailure, RequestUnknown
from .util import now, write_json


def run_passes(tasks, process, *, passes, concurrency, stage, path, safe_error, on_success=None, merge_round=None, stop_on_unknown=False):
    results, errors, history = {}, {}, []
    pending_ids = list(range(len(tasks)))
    started = time.monotonic()

    def record(pass_number, state="running"):
        unknown = sum(e["state"] == "unknown" for e in errors.values())
        progress = dict(pass_number=pass_number, max_passes=passes, completed=len(results) + len(errors),
                        total=len(tasks), successful=len(results), failed=len(errors)-unknown, unknown=unknown,
                        remaining=len(tasks)-len(results), elapsed_seconds=round(time.monotonic()-started, 3))
        if merge_round is not None:
            progress["round"] = merge_round
        write_json(path, dict(state=state, updated_at=now(), **progress, attempts=history,
                             errors=[dict(batch=i+1, **error) for i, error in sorted(errors.items())]))
        emit("progress", stage=stage, **progress,
             message=f"第 {pass_number}/{passes} 轮：成功 {len(results)}/{len(tasks)}，未完成 {len(tasks)-len(results)}")

    pass_number = 1
    for next_pass in range(1, passes + 1):
        if not pending_ids:
            break
        pass_number = next_pass
        record(pass_number)
        print(f"PASS {stage} {pass_number}/{passes} pending={len(pending_ids)} successful={len(results)}/{len(tasks)}", flush=True)
        retry_ids = []

        def accept(i, result=None, error=None):
            if error is None:
                results[i] = result
                errors.pop(i, None)
                if on_success:
                    on_success(i, result)
                history.append(dict(batch=i+1, pass_number=pass_number, state="success"))
            else:
                state = "unknown" if isinstance(error, RequestUnknown) else "failed"
                errors[i] = dict(state=state, error=safe_error(error))
                history.append(dict(batch=i+1, pass_number=pass_number, **errors[i]))
                if (isinstance(error, NonRetryableModelFailure)
                        or stop_on_unknown and isinstance(error, RequestUnknown)
                        or not isinstance(error, (RuntimeError, ValueError))):
                    record(pass_number, "partial")
                    raise error
                if not isinstance(error, (RequestUnknown, BudgetError)):
                    retry_ids.append(i)
            record(pass_number)
            print(f"TASK {stage} {len(results)+len(errors)}/{len(tasks)} ({len(results)} successful, pass {pass_number}/{passes})", flush=True)

        if concurrency == 1:
            for i in pending_ids:
                try:
                    result = process(i, tasks[i], pass_number)
                except Exception as error:
                    accept(i, error=error)
                else:
                    accept(i, result=result)
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                iterator = iter(pending_ids)
                pending = {}
                def refill():
                    while len(pending) < concurrency:
                        i = next(iterator, None)
                        if i is None:
                            break
                        pending[pool.submit(process, i, tasks[i], pass_number)] = i
                refill()
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        i = pending.pop(future)
                        try:
                            result = future.result()
                        except Exception as error:
                            accept(i, error=error)
                        else:
                            accept(i, result=result)
                    refill()
        pending_ids = sorted(retry_ids)
    record(pass_number, "partial" if errors else "success")
    return results, errors
