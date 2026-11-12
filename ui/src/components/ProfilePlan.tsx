import { useMemo, useState } from "react";

import { estimatedRowsText, operatorInsights, profileMetricText, type ParsedProfile } from "../lib/queryProfile";

export function ProfilePlan({ profile }: { profile: ParsedProfile }) {
  const [order, setOrder] = useState("plan");
  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(new Set());
  const insights = useMemo(() => operatorInsights(profile.operators), [profile.operators]);
  const visible = [];
  let hiddenUntil = 0;
  for (const item of insights) {
    if (order === "plan" && item.index < hiddenUntil) continue;
    visible.push(item);
    if (order === "plan" && collapsed.has(item.index)) hiddenUntil = item.end;
  }
  if (order === "slow") visible.sort((a, b) => b.fraction - a.fraction || a.index - b.index);
  return (
    <>
      <h4>Execution plan</h4>
      <p className="page-sub" id="profile-timing-help">
        Bars compare operators with the slowest operator. Operator timing and CPU measurements can overlap across
        parallel work; they are not percentages of total elapsed time. Missing metrics are shown as —.
      </p>
      <p className="profile-legend" aria-label="Operator timing colors">
        <span className="profile-duration-long">Long: ≥50% of slowest</span>
        <span className="profile-duration-medium">Medium: 10–50%</span>
        <span className="profile-duration-short">Short: &lt;10%</span>
      </p>
      <div className="profile-controls">
        <label>
          Operator order{" "}
          <select value={order} onChange={(event) => setOrder(event.target.value)}>
            <option value="plan">Plan order</option>
            <option value="slow">Slowest first</option>
          </select>
        </label>
        <button
          type="button"
          className="btn btn-ghost"
          disabled={order !== "plan" || profile.operatorsTruncated}
          onClick={() =>
            setCollapsed(
              new Set(insights.filter((item) => item.small && item.end > item.index + 1).map((item) => item.index)),
            )
          }
        >
          Collapse small subtrees
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          disabled={collapsed.size === 0}
          onClick={() => setCollapsed(new Set())}
        >
          Expand all
        </button>
      </div>
      <p className="page-sub">
        Highlighted: up to three slow operators and row estimates off by 10× or more. Small subtrees total at most 1% of
        summed operator time.
      </p>
      {profile.operators.length > 0 ? (
        <div className="monitor-table-wrap">
          <table
            className="data-table profile-plan"
            data-testid="query-profile-plan"
            aria-describedby="profile-timing-help"
          >
            <thead>
              <tr>
                <th>Operator</th>
                <th className="profile-time-heading">Time</th>
                <th>Actual rows</th>
                <th>Estimated rows</th>
                <th>Rows scanned</th>
                <th>Row groups scanned / total</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((item) => {
                const op = profile.operators[item.index]!;
                const hasChildren = item.end > item.index + 1;
                return (
                  <tr
                    key={item.index}
                    className={`profile-duration-${item.durationBand} ${item.hot ? "profile-hot" : item.small ? "profile-small" : ""}`}
                  >
                    <td
                      className="mono profile-operator"
                      style={{ paddingLeft: 10 + (order === "plan" ? Math.min(op.depth, 8) * 10 : 0) }}
                    >
                      {hasChildren && order === "plan" && (
                        <button
                          type="button"
                          className="profile-collapse"
                          aria-label={`${collapsed.has(item.index) ? "Expand" : "Collapse"} ${op.name} subtree`}
                          aria-expanded={!collapsed.has(item.index)}
                          onClick={() =>
                            setCollapsed((current) => {
                              const next = new Set(current);
                              if (next.has(item.index)) next.delete(item.index);
                              else next.add(item.index);
                              return next;
                            })
                          }
                        >
                          {collapsed.has(item.index) ? "▸" : "▾"}
                        </button>
                      )}
                      {op.name}
                      {item.hot && <span className="profile-badge">Slow operator</span>}
                      {order === "plan" && collapsed.has(item.index) && (
                        <small>{item.end - item.index - 1} hidden</small>
                      )}
                    </td>
                    <td className="profile-time">
                      <span className="profile-time-value">
                        {op.timing === undefined
                          ? "—"
                          : `${(op.timing * 1000).toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 })} ms`}
                      </span>
                      {op.timing !== undefined && (
                        <span className="profile-time-track" aria-hidden="true">
                          <span style={{ width: `${item.fraction * 100}%`, opacity: item.fraction > 0 ? 1 : 0 }} />
                        </span>
                      )}
                    </td>
                    <td>{op.rows?.toLocaleString() ?? "—"}</td>
                    <td className={item.estimateError ? "profile-estimate-error" : ""}>
                      {estimatedRowsText(op.estimatedRows)}
                      {item.estimateError && <span className="profile-badge">{item.estimateError}</span>}
                    </td>
                    <td>{op.scanned?.toLocaleString() ?? "—"}</td>
                    <td>
                      {op.rowGroups?.toLocaleString() ?? "—"} / {op.totalRowGroups?.toLocaleString() ?? "—"}
                    </td>
                    <td>
                      <details>
                        <summary>Operator details</summary>
                        <dl className="profile-metrics">
                          {op.metrics.map((metric) => (
                            <div key={metric.name}>
                              <dt className="mono">{metric.name}</dt>
                              <dd>{profileMetricText(metric.value, metric.unit)}</dd>
                            </div>
                          ))}
                        </dl>
                        <pre className="value-text">{JSON.stringify(op.extra, null, 2)}</pre>
                      </details>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p>No operator metrics were captured.</p>
      )}
    </>
  );
}
