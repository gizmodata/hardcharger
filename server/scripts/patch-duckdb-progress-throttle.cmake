# Makes DuckDB's progress estimate cost nothing for a statement shorter than
# progress_bar_time, and little for a longer one.
#
# With enable_progress_bar on (Hardcharger turns it on for every session, so the SQL Monitor,
# the Web UI and the CLI can show an estimate), DuckDB recomputes the estimate after every
# task slice the client thread runs: ProgressBar::Update walks every pipeline of the plan
# (Executor::GetPipelinesProgress) holding the executor's lock, which the worker threads
# need to schedule and finish their own tasks. progress_bar_time only delays printing a bar,
# not this work. On a plan with many pipelines (a 30-way UNION ALL, many scalar subqueries)
# the walks and the lock waits they cause stalled statements by 150-1350 ms and multiplied
# the server's CPU time (DB Olympics q311: median 112 ms against 11.5 ms with the estimate
# off). With the patch, Update computes nothing before progress_bar_time has passed and then
# at most once per 100 ms; printing and the estimate's value are unchanged otherwise.
#
# A source that no longer matches is a build error, never a silent skip.
#
# Run as: cmake -DDUCKDB_SOURCE_DIR=<dir> -P patch-duckdb-progress-throttle.cmake

set(HEADER_FILE "${DUCKDB_SOURCE_DIR}/src/include/duckdb/common/progress_bar/progress_bar.hpp")
set(SOURCE_FILE "${DUCKDB_SOURCE_DIR}/src/common/progress_bar/progress_bar.cpp")
set(MARKER "next_update_ms")

file(READ "${HEADER_FILE}" HEADER)
file(READ "${SOURCE_FILE}" SOURCE)
string(FIND "${HEADER}" "${MARKER}" header_done)
string(FIND "${SOURCE}" "${MARKER}" source_done)
if(NOT header_done EQUAL -1 AND NOT source_done EQUAL -1)
  message(STATUS "patch-duckdb-progress-throttle: already patched")
  return()
endif()
if(NOT header_done EQUAL -1 OR NOT source_done EQUAL -1)
  message(FATAL_ERROR "patch-duckdb-progress-throttle: only one of ${HEADER_FILE} and ${SOURCE_FILE} is patched")
endif()

# Replaces `before` (which must occur exactly once in the variable named by `var`) with `after`.
function(replace_once var before after what)
  string(FIND "${${var}}" "${before}" first_at)
  string(FIND "${${var}}" "${before}" last_at REVERSE)
  if(first_at EQUAL -1 OR NOT first_at EQUAL last_at)
    message(FATAL_ERROR "patch-duckdb-progress-throttle: ${what} was not found exactly once; "
                        "update server/scripts/patch-duckdb-progress-throttle.cmake for this DuckDB")
  endif()
  string(REPLACE "${before}" "${after}" result "${${var}}")
  set(${var} "${result}" PARENT_SCOPE)
endfunction()

# 1. The member: a uint32_t in the padding after the two bools, so the class keeps its size.
replace_once(HEADER [=[
	//! Whether the bar has already finished
	bool finished = false;
};]=] [=[
	//! Whether the bar has already finished
	bool finished = false;
	//! Hardcharger: the query time (ms) before which Update computes nothing (progress_bar_time,
	//! then 100 ms after each computation). server/scripts/patch-duckdb-progress-throttle.cmake.
	uint32_t next_update_ms = 0;
};]=] "the end of class ProgressBar")

# 2. Start: the first computation waits for progress_bar_time.
replace_once(SOURCE [=[
void ProgressBar::Start() {
	profiler.Start();
	query_progress.Initialize();
	supported = true;
}]=] [=[
void ProgressBar::Start() {
	profiler.Start();
	query_progress.Initialize();
	supported = true;
	next_update_ms = static_cast<uint32_t>(MinValue<idx_t>(show_progress_after, NumericLimits<uint32_t>::Maximum()));
}]=] "ProgressBar::Start")

# 3. Update: compute only when due; a final update still finishes a printed bar.
replace_once(SOURCE [=[
	ProgressData progress;
	idx_t invalid_pipelines = executor.GetPipelinesProgress(progress);

	double new_percentage = 0.0;
	if (invalid_pipelines == 0 && progress.IsValid()) {
		if (progress.total > 1e15) {
			progress.Normalize(1e15);
		}
		query_progress.rows_processed = idx_t(progress.done);
		query_progress.total_rows_to_process = idx_t(progress.total);
		new_percentage = progress.ProgressDone() * 100;
	}

	if (!final && invalid_pipelines > 0) {
		return;
	}

	if (new_percentage > query_progress.percentage) {
		query_progress.percentage = new_percentage;
	}
	if (ShouldPrint(final)) {]=] [=[
	// Hardcharger: computing the estimate walks every pipeline under the executor's lock, which
	// the worker threads need too; done after every task it stalled plans with many pipelines.
	// Nothing before progress_bar_time, then at most every 100 ms.
	// server/scripts/patch-duckdb-progress-throttle.cmake.
	static constexpr double UPDATE_INTERVAL_MS = 100;
	const double elapsed_ms = profiler.Elapsed() * 1000.0;
	if (elapsed_ms < static_cast<double>(next_update_ms)) {
		if (!final) {
			return;
		}
	} else {
		next_update_ms = static_cast<uint32_t>(
		    MinValue<double>(elapsed_ms + UPDATE_INTERVAL_MS, static_cast<double>(NumericLimits<uint32_t>::Maximum())));

		ProgressData progress;
		idx_t invalid_pipelines = executor.GetPipelinesProgress(progress);

		double new_percentage = 0.0;
		if (invalid_pipelines == 0 && progress.IsValid()) {
			if (progress.total > 1e15) {
				progress.Normalize(1e15);
			}
			query_progress.rows_processed = idx_t(progress.done);
			query_progress.total_rows_to_process = idx_t(progress.total);
			new_percentage = progress.ProgressDone() * 100;
		}

		if (!final && invalid_pipelines > 0) {
			return;
		}

		if (new_percentage > query_progress.percentage) {
			query_progress.percentage = new_percentage;
		}
	}
	if (ShouldPrint(final)) {]=] "the body of ProgressBar::Update")

file(WRITE "${HEADER_FILE}" "${HEADER}")
file(WRITE "${SOURCE_FILE}" "${SOURCE}")
message(STATUS "patch-duckdb-progress-throttle: progress is computed after progress_bar_time, at most every 100 ms")
