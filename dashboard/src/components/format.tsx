import type { Position } from '../api/contracts';
export const number = (value: number | null | undefined, suffix = '') => value == null ? 'Unknown' : `${Number(value.toFixed(2))}${suffix}`;
export const position = (value?: Position | null) => value ? `${value.latitude.toFixed(4)}, ${value.longitude.toFixed(4)}` : 'Unknown';
export const time = (value?: string | null) => value ? new Date(value).toLocaleTimeString('en-SG', { timeZone: 'Asia/Singapore', hour12: false }) : 'Not received';
export function Badge({ value }: { value: string }) { return <span className={`badge ${value.toLowerCase()}`}>{value.replaceAll('_', ' ')}</span>; }
