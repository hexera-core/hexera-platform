export const HEXERA_API_PREFIX = "/api/v1" as const;

type PathSegment = string | number;

function segment(value: PathSegment): string {
  return encodeURIComponent(String(value));
}

export const hexeraApiRoutes = {
  chatHistory: (sessionId: PathSegment) =>
    `${HEXERA_API_PREFIX}/chat/history/${segment(sessionId)}`,
  chatMessage: `${HEXERA_API_PREFIX}/chat/message`,
  clientConfig: `${HEXERA_API_PREFIX}/client-config`,
  simulation: (jobId: PathSegment) =>
    `${HEXERA_API_PREFIX}/simulation/${segment(jobId)}`,
  simulationDispute: (jobId: PathSegment) =>
    `${HEXERA_API_PREFIX}/simulation/${segment(jobId)}/dispute`,
  simulationSurface: (jobId: PathSegment) =>
    `${HEXERA_API_PREFIX}/simulation/${segment(jobId)}/surface`,
  uploadStepFile: `${HEXERA_API_PREFIX}/upload/step-file`,
  wsStream: (jobId: PathSegment) =>
    `${HEXERA_API_PREFIX}/ws/${segment(jobId)}/stream`,
  wsTicket: `${HEXERA_API_PREFIX}/ws/ticket`,
} as const;

export type HexeraApiRoute =
  | (typeof hexeraApiRoutes)[keyof typeof hexeraApiRoutes]
  | ReturnType<(typeof hexeraApiRoutes)["chatHistory"]>
  | ReturnType<(typeof hexeraApiRoutes)["simulation"]>
  | ReturnType<(typeof hexeraApiRoutes)["simulationDispute"]>
  | ReturnType<(typeof hexeraApiRoutes)["simulationSurface"]>
  | ReturnType<(typeof hexeraApiRoutes)["wsStream"]>;
