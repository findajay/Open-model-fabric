import { OmfClient } from "@omf/sdk";

const TOKEN_KEY = "omf.apiToken";

/**
 * The only credential the browser ever holds is the user's own token for this platform's
 * API (when authentication is enabled). Provider/infrastructure credentials never leave
 * the backend. The token lives in sessionStorage so it is dropped with the tab.
 */
export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // storage unavailable (private mode): token stays in memory only for this request
  }
}

export const api = new OmfClient({
  baseUrl: import.meta.env.VITE_OMF_API_URL ?? "",
  token: getToken,
});
