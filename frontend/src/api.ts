export type Identity = { username: string; csrf_token: string };
export type SystemInfo = {
  organization: string;
  phase: number;
  frameworks: string[];
  inventory_seeded: boolean;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
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
