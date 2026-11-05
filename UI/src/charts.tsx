import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { CH } from './ui';

export const SEVERITY_COLORS: Record<string, string> = {
  critical: '#DC2626', high: '#EA580C', medium: '#D97706', low: '#16A34A', unknown: '#94A3B8',
};
const PALETTE = ['#0E8C8F', '#7CD1CB', '#D97706', '#DC2626', '#6366F1', '#94A3B8'];

const Box = ({ height, children }: { height: number; children: any }) =>
  <div className="chartbox" style={{ height, minWidth: 0 }}>
    <ResponsiveContainer width="100%" height="100%" minWidth={0}>{children}</ResponsiveContainer></div>;

export function Donut({ data, height = 220, colors }: { data: { name: string; value: number }[]; height?: number; colors?: Record<string, string> }) {
  const rows = data.filter(item => item.value > 0);
  if (!rows.length) return <div className="empty">No data to chart yet.</div>;
  return <Box height={height}><PieChart>
    <Pie data={rows} dataKey="value" nameKey="name" innerRadius="58%" outerRadius="84%" paddingAngle={3} {...CH.anim} stroke="none">
      {rows.map((row, index) => <Cell key={row.name} fill={colors?.[row.name] || PALETTE[index % PALETTE.length]} />)}
    </Pie>
    <Tooltip {...CH.tip} /><Legend iconType="circle" wrapperStyle={{ fontSize: 12 }} />
  </PieChart></Box>;
}

export function TrendArea({ data, keys, height = 230 }: { data: Record<string, number | string>[]; keys: string[]; height?: number }) {
  if (!data.length) return <div className="empty">No timestamped records to chart.</div>;
  return <Box height={height}><AreaChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
    <defs>{keys.map((key, index) => <linearGradient key={key} id={`g-${key}`} x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stopColor={SEVERITY_COLORS[key] || PALETTE[index]} stopOpacity={0.45} />
      <stop offset="100%" stopColor={SEVERITY_COLORS[key] || PALETTE[index]} stopOpacity={0.02} /></linearGradient>)}</defs>
    <CartesianGrid stroke={CH.grid} vertical={false} />
    <XAxis dataKey="label" stroke={CH.axis} fontSize={11} tickLine={false} />
    <YAxis stroke={CH.axis} fontSize={11} allowDecimals={false} tickLine={false} axisLine={false} />
    <Tooltip {...CH.tip} /><Legend iconType="circle" wrapperStyle={{ fontSize: 12 }} />
    {keys.map((key, index) => <Area key={key} type="monotone" dataKey={key} stackId="1" stroke={SEVERITY_COLORS[key] || PALETTE[index]}
      strokeWidth={2} fill={`url(#g-${key})`} {...CH.anim} />)}
  </AreaChart></Box>;
}

export function Bars({ data, keys, height = 220, unit = '', layout = 'vertical' }: {
  data: Record<string, number | string | null>[]; keys: { key: string; label: string; color?: string }[];
  height?: number; unit?: string; layout?: 'vertical' | 'horizontal';
}) {
  if (!data.length) return <div className="empty">No data to chart yet.</div>;
  const horizontal = layout === 'horizontal';
  return <Box height={height}><BarChart data={data} layout={horizontal ? 'vertical' : 'horizontal'} margin={{ top: 8, right: 12, left: horizontal ? 24 : -14, bottom: 0 }}>
    <CartesianGrid stroke={CH.grid} horizontal={!horizontal} vertical={horizontal} />
    {horizontal
      ? <><XAxis type="number" stroke={CH.axis} fontSize={11} tickLine={false} /><YAxis type="category" dataKey="name" stroke={CH.axis} fontSize={11} width={110} tickLine={false} /></>
      : <><XAxis dataKey="name" stroke={CH.axis} fontSize={11} tickLine={false} /><YAxis stroke={CH.axis} fontSize={11} tickLine={false} axisLine={false} /></>}
    <Tooltip {...CH.tip} formatter={(value: any) => (value == null ? 'Unavailable' : `${value}${unit}`)} />
    {keys.length > 1 && <Legend iconType="circle" wrapperStyle={{ fontSize: 12 }} />}
    {keys.map((item, index) => <Bar key={item.key} dataKey={item.key} name={item.label} fill={item.color || PALETTE[index]} radius={[6, 6, 0, 0]} {...CH.anim} />)}
  </BarChart></Box>;
}
