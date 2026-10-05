/**
 * Quote one CSV cell. A cell starting with = + - @ tab or CR would be run as a formula by
 * spreadsheet apps (emails from the public redeem form are attacker-controlled), so it gets
 * a leading single quote. A lone "-" is a plain placeholder and stays as is.
 */
export function csvField(value: string | number | null | undefined): string {
  let s = value === null || value === undefined ? '' : String(value);
  if (s !== '-' && /^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return `"${s.replace(/"/g, '""')}"`;
}
