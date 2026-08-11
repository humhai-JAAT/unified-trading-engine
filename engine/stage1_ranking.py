"""Stage 1 — ranking data layer. See Architecture.md's "Stage 1 — ranking data
layer" for the full design this implements.

Fetches today's %-change for every stock in bot_400's universe (the biggest,
superset universe — Nifty500 minus Nifty100) exactly ONCE per cycle, split
across parallel workers on multiple broker accounts, then lets both
universe-bots derive their own top-N from this ONE shared result — no
per-universe API calls.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd

from common.helpers import get_logger
from engine.broker_accounts import BrokerAccount, QuoteResult, get_configured_accounts

logger = get_logger(__name__)

WORKERS_PER_ACCOUNT = 3


@dataclass
class Stage1Result:
    rank_list: pd.DataFrame          # columns: symbol, last_price, pct_change — sorted desc
    warnings: list[str] = field(default_factory=list)
    chunks_total: int = 0
    chunks_fallback_used: int = 0
    chunks_failed: int = 0


def _chunk(symbols: list[str], n_chunks: int) -> list[list[str]]:
    """Splits symbols into n_chunks roughly-equal pieces (e.g. 751 into 6 chunks
    of ~125 each) — no chunk is empty as long as n_chunks <= len(symbols)."""
    if n_chunks <= 0:
        return [symbols]
    size = len(symbols) // n_chunks
    remainder = len(symbols) % n_chunks
    chunks, start = [], 0
    for i in range(n_chunks):
        end = start + size + (1 if i < remainder else 0)
        if start < end:
            chunks.append(symbols[start:end])
        start = end
    return chunks


def _build_workers(primary_accounts: list[BrokerAccount]) -> list[BrokerAccount]:
    """Each account gets WORKERS_PER_ACCOUNT worker "slots" — but since workers
    sharing one account all go through that SAME account's AccountRateLimiter,
    this is really about how many chunks get assigned to each account
    concurrently, not genuinely independent parallelism within an account. The
    real cross-account parallelism comes from having 2+ accounts in the list."""
    workers = []
    for account in primary_accounts:
        workers.extend([account] * WORKERS_PER_ACCOUNT)
    return workers


def _fetch_chunk(account: BrokerAccount, chunk: list[str]) -> list[QuoteResult]:
    return account.fetch_quotes_batch(chunk)


def fetch_ranking_data(symbols: list[str], fallback_accounts: list[BrokerAccount] | None = None,
                        primary_accounts: list[BrokerAccount] | None = None) -> Stage1Result:
    """Fetches %-change for every symbol in `symbols`, using `primary_accounts`
    (defaults to configured Groww accounts) for the initial parallel pass, and
    retrying ONLY failed chunks via `fallback_accounts` (defaults to configured
    Angel One accounts) — never re-fetching chunks that already succeeded. See
    Architecture.md's "Fallback (chunk-level, not whole-list)"."""
    accounts = get_configured_accounts()
    if primary_accounts is None:
        primary_accounts = accounts["groww"] or accounts["angelone"]
    if fallback_accounts is None:
        fallback_accounts = accounts["angelone"]

    if not primary_accounts:
        return Stage1Result(rank_list=pd.DataFrame(columns=["symbol", "last_price", "pct_change"]),
                             warnings=["No broker accounts configured for Stage 1 ranking fetch."])

    workers = _build_workers(primary_accounts)
    chunks = _chunk(symbols, len(workers))

    result_rows: list[QuoteResult] = []
    warnings: list[str] = []
    fallback_used = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        futures = {
            executor.submit(_fetch_chunk, workers[i], chunk): (i, chunk)
            for i, chunk in enumerate(chunks)
        }
        failed_chunks: list[tuple[int, list[str]]] = []
        for future in as_completed(futures):
            idx, chunk = futures[future]
            try:
                rows = future.result()
            except Exception as e:
                logger.warning(f"Stage 1 chunk {idx} raised: {e}")
                rows = []
            # A chunk that returned strictly fewer symbols than requested (or none
            # at all) is treated as a partial/total failure for THIS chunk only —
            # the missing symbols get one fallback attempt, not the whole chunk
            # re-fetched, since some symbols may have genuinely succeeded.
            returned_symbols = {r.symbol for r in rows}
            missing = [s for s in chunk if s not in returned_symbols]
            result_rows.extend(rows)
            if missing:
                failed_chunks.append((idx, missing))

        if failed_chunks and fallback_accounts:
            fallback_account = fallback_accounts[0]
            for idx, missing_symbols in failed_chunks:
                try:
                    recovered = fallback_account.fetch_quotes_batch(missing_symbols)
                except Exception as e:
                    logger.warning(f"Stage 1 fallback for chunk {idx} raised: {e}")
                    recovered = []
                recovered_symbols = {r.symbol for r in recovered}
                still_missing = [s for s in missing_symbols if s not in recovered_symbols]
                result_rows.extend(recovered)
                if recovered:
                    fallback_used += 1
                if still_missing:
                    failed += 1
                    warnings.append(
                        f"Chunk {idx}: {len(still_missing)}/{len(missing_symbols)} symbols "
                        f"unavailable even after fallback ({fallback_account.account_id})."
                    )
        elif failed_chunks:
            failed += len(failed_chunks)
            warnings.append(
                f"{len(failed_chunks)} chunk(s) had missing symbols and no fallback account "
                f"was configured to retry them."
            )

    if not result_rows:
        rank_list = pd.DataFrame(columns=["symbol", "last_price", "pct_change"])
    else:
        rank_list = pd.DataFrame([r.__dict__ for r in result_rows])
        rank_list = rank_list.drop_duplicates(subset="symbol", keep="first")
        rank_list = rank_list.sort_values("pct_change", ascending=False).reset_index(drop=True)

    return Stage1Result(
        rank_list=rank_list, warnings=warnings, chunks_total=len(chunks),
        chunks_fallback_used=fallback_used, chunks_failed=failed,
    )
