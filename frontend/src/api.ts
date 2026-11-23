export type Identity = { username: string; csrf_token: string; role?: string; employee_id?: string | null };
export type SystemInfo = {
  organization: string;
  phase: number;
  frameworks: string[];
  inventory_seeded: boolean;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function downloadFile(path: string): Promise<void> {
  const response = await fetch(`/api${path}`, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, typeof body?.detail === 'string' ? body.detail : 'Download failed. Please try again.');
  }
  const filename = response.headers.get('Content-Disposition')?.match(/filename="([a-zA-Z0-9.-]+)"/)?.[1] ?? 'myl-report.yaml';
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url; anchor.download = filename; document.body.appendChild(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', ...options.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, typeof body?.detail === 'string' ? body.detail : 'The request could not be completed. Please try again.');
  }
  return response.status === 204 ? undefined as T : response.json();
}
