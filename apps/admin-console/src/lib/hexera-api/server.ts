import { createHexeraApiClient } from "@hexera/api-client";

export function getHexeraApiClient() {
  return createHexeraApiClient({
    baseUrl: process.env.HEXERA_API_BASE_URL,
  });
}
