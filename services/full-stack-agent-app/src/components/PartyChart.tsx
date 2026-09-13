// services/full-stack-agent-app/src/components/PartyChart.tsx
"use client";

import dynamic from "next/dynamic";
import type { PartyDistributionChart } from "@/lib/types";

// Plotly needs window/document, so it must never render on the server.
const Plot = dynamic(() => import("react-plotly.js"), { ssr: false });

export default function PartyChart({
  chartData,
}: {
  chartData: PartyDistributionChart | null | undefined;
}) {
  if (!chartData || !chartData.distributions || chartData.distributions.length === 0) return null;

  const distributions = chartData.distributions;
  const isComparison = distributions.length > 1;
  const chartType = chartData.chart_type || "bar";

  // Group rows by Period label, mirroring build_party_chart()'s
  // Party/Count/Period rows in the old Streamlit app -- one Plotly
  // trace per period so a comparison renders as a grouped bar chart.
  const byPeriod: Record<string, { parties: string[]; counts: number[] }> = {};
  for (const dist of distributions) {
    const label = dist.wahlperiode ? `WP ${dist.wahlperiode}` : dist.date_range || "—";
    const counts = dist.counts || {};
    byPeriod[label] = { parties: Object.keys(counts), counts: Object.values(counts) };
  }

  const traces = Object.entries(byPeriod).map(([label, data]) => ({
    x: data.parties,
    y: data.counts,
    name: label,
    type: chartType === "scatter" ? "scatter" : "bar",
    mode: chartType === "scatter" ? "markers" : undefined,
  }));

  const title = isComparison
    ? "Party Distribution Comparison"
    : `Party Distribution (Wahlperiode ${distributions[0]?.wahlperiode ?? ""})`;

  return (
    <div className="mt-3 rounded-lg border border-neutral-200 bg-white p-2">
      <Plot
        data={traces as any}
        layout={{
          title,
          barmode: isComparison ? "group" : undefined,
          autosize: true,
          margin: { t: 40, l: 40, r: 20, b: 40 },
        }}
        useResizeHandler
        style={{ width: "100%", height: "380px" }}
        config={{ displayModeBar: false }}
      />
    </div>
  );
}