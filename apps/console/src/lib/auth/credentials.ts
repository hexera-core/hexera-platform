import { randomBytes, scrypt as scryptCallback, timingSafeEqual } from "node:crypto";

const SCRYPT_N = 16384;
const SCRYPT_R = 8;
const SCRYPT_P = 1;
const SCRYPT_KEY_LENGTH = 64;
const SCRYPT_MAXMEM = 64 * 1024 * 1024;

type ConsoleAuthUserConfig = {
  email?: unknown;
  name?: unknown;
  passwordHash?: unknown;
};

type ConsoleAuthEnv = Record<string, string | undefined>;

export type ConsoleAuthUser = {
  id: string;
  email: string;
  name: string;
};

export async function hashConsolePassword(
  password: string,
  salt = randomBytes(16).toString("base64url"),
): Promise<string> {
  const key = await deriveScryptKey(password, salt, SCRYPT_KEY_LENGTH, {
    n: SCRYPT_N,
    r: SCRYPT_R,
    p: SCRYPT_P,
  });

  return [
    "scrypt",
    String(SCRYPT_N),
    String(SCRYPT_R),
    String(SCRYPT_P),
    salt,
    key.toString("base64url"),
  ].join(":");
}

export async function verifyConsolePassword(
  password: string,
  passwordHash: string,
): Promise<boolean> {
  const parts = passwordHash.split(":");
  if (parts.length !== 6 || parts[0] !== "scrypt") {
    return false;
  }

  const [, nValue, rValue, pValue, salt, encodedKey] = parts;
  const params = {
    n: Number(nValue),
    r: Number(rValue),
    p: Number(pValue),
  };

  if (!isValidScryptParams(params)) {
    return false;
  }

  let expectedKey: Buffer;
  try {
    expectedKey = Buffer.from(encodedKey, "base64url");
  } catch {
    return false;
  }

  if (expectedKey.length === 0) {
    return false;
  }

  let actualKey: Buffer;
  try {
    actualKey = await deriveScryptKey(password, salt, expectedKey.length, params);
  } catch {
    return false;
  }

  return actualKey.length === expectedKey.length && timingSafeEqual(actualKey, expectedKey);
}

export async function verifyConsoleCredentials(
  credentials: Partial<Record<"email" | "password", unknown>>,
  env: ConsoleAuthEnv = process.env,
): Promise<ConsoleAuthUser | null> {
  const email = normalizeEmail(credentials.email);
  const password = credentials.password;
  if (!email || typeof password !== "string") {
    return null;
  }

  const user = parseConsoleAuthUsers(env.CONSOLE_AUTH_USERS).find(
    (candidate) => candidate.email === email,
  );
  if (!user) {
    return null;
  }

  const passwordMatches = await verifyConsolePassword(password, user.passwordHash);
  if (!passwordMatches) {
    return null;
  }

  return {
    id: user.email,
    email: user.email,
    name: user.name || user.email,
  };
}

function parseConsoleAuthUsers(rawUsers: string | undefined): Array<{
  email: string;
  name: string;
  passwordHash: string;
}> {
  if (!rawUsers) {
    return [];
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(rawUsers);
  } catch {
    return [];
  }

  if (!Array.isArray(parsed)) {
    return [];
  }

  return parsed.flatMap((rawUser: ConsoleAuthUserConfig) => {
    const email = normalizeEmail(rawUser.email);
    if (!email || typeof rawUser.passwordHash !== "string") {
      return [];
    }

    return [
      {
        email,
        name: typeof rawUser.name === "string" ? rawUser.name.trim() : "",
        passwordHash: rawUser.passwordHash,
      },
    ];
  });
}

function normalizeEmail(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }

  const normalized = value.trim().toLowerCase();
  return normalized.includes("@") ? normalized : null;
}

async function deriveScryptKey(
  password: string,
  salt: string,
  keyLength: number,
  params: { n: number; r: number; p: number },
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    scryptCallback(
      password,
      salt,
      keyLength,
      {
        N: params.n,
        r: params.r,
        p: params.p,
        maxmem: SCRYPT_MAXMEM,
      },
      (error, derivedKey) => {
        if (error) {
          reject(error);
          return;
        }

        resolve(derivedKey);
      },
    );
  });
}

function isValidScryptParams(params: { n: number; r: number; p: number }): boolean {
  return (
    Number.isSafeInteger(params.n) &&
    Number.isSafeInteger(params.r) &&
    Number.isSafeInteger(params.p) &&
    params.n > 1 &&
    params.n <= 4294967295 &&
    params.r > 0 &&
    params.p > 0
  );
}
