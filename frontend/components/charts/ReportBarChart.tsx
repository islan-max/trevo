"use client";

import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatBRL } from "@/lib/format";

export type ReportBarSeries = {
  dataKey: string;
  name: string;
  fill: string;
  radius?: [number, number, number, number];
  animationDuration?: number;
};

export type ReportChartTheme = {
  grid: string;
  text: string;
  stroke: string;
  cursorFill: string;
  tooltipStyle: {
    backgroundColor: string;
    border: string;
    borderRadius: number;
    color: string;
  };
};

type ReportBarChartProps = {
  data: Record<string, unknown>[];
  bars: ReportBarSeries[];
  xAxisDataKey: string;
  theme: ReportChartTheme;
  margin?: { left?: number; right?: number; bottom?: number };
  angledLabels?: boolean;
  showLegend?: boolean;
};

/**
 * Vive em módulo próprio para o recharts (~140 kB) sair do bundle inicial de
 * /relatorios e ser carregado sob demanda — ver relatorios/page.tsx (PERF-05).
 */
export default function ReportBarChart({
  data,
  bars,
  xAxisDataKey,
  theme,
  margin,
  angledLabels,
  showLegend
}: ReportBarChartProps) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={margin || { left: 4, right: 12, bottom: 18 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
        <XAxis
          dataKey={xAxisDataKey}
          tickLine={false}
          axisLine={false}
          tick={{ fontSize: 11, fill: theme.text }}
          interval={angledLabels ? 0 : undefined}
          angle={angledLabels ? -18 : undefined}
          textAnchor={angledLabels ? "end" : undefined}
        />
        <YAxis width={54} tickFormatter={(value) => `R$${Number(value) / 1000}k`} tickLine={false} axisLine={false} tick={{ fill: theme.text }} />
        <Tooltip contentStyle={theme.tooltipStyle} cursor={{ fill: theme.cursorFill }} formatter={(value) => formatBRL(Number(value))} />
        {showLegend ? <Legend /> : null}
        {bars.map((bar) => (
          <Bar
            key={bar.dataKey}
            dataKey={bar.dataKey}
            name={bar.name}
            fill={bar.fill}
            radius={bar.radius || [6, 6, 0, 0]}
            activeBar={{ fillOpacity: 0.88, stroke: theme.stroke, strokeWidth: 2 }}
            isAnimationActive
            animationDuration={bar.animationDuration ?? 650}
            animationEasing="ease-out"
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
