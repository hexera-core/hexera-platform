import { KeyManagementServiceClient } from "@google-cloud/kms";

// ENVELOPE ENCRYPTION FOR THE MAILBOX CREDENTIAL.
//
// `oauth_tokens.refresh_token` is long-lived access to the sending mailbox. On a laptop it sat in
// a local SQLite file that never left the machine; hosted, it sits in a shared database whose
// backups, replicas and dumps all inherit whatever is in that column. Without this, a database
// dump is a mailbox compromise - a much larger blast radius than "the contact list leaked".
//
// KMS never sees the plaintext twice and never hands out the key: encrypt/decrypt happen at the
// service, and the admin identity holds cryptoKeyEncrypterDecrypter on one key and nothing wider.
//
// STORED WITH A PREFIX so a value can be told apart from a plaintext one written before this
// existed. A row imported from the laptop is readable until it is rewritten, and reading it is not
// a silent failure - `decrypt` returns it unchanged and says so by its absence of prefix.

const PREFIX = "kms:v1:";

export type KmsCipher = Pick<KeyManagementServiceClient, "encrypt" | "decrypt">;

let client: KeyManagementServiceClient | null = null;

export function getKmsClient(): KeyManagementServiceClient {
  client ??= new KeyManagementServiceClient();
  return client;
}

export function kmsKeyName(env: Record<string, string | undefined>): string | null {
  return env.OUTREACH_KMS_KEY?.trim() || null;
}

export async function encryptSecret(
  plaintext: string | null | undefined,
  keyName: string | null,
  cipher: KmsCipher = getKmsClient(),
): Promise<string | null> {
  if (plaintext === null || plaintext === undefined || plaintext === "") return null;
  if (!keyName) {
    throw new Error(
      "OUTREACH_KMS_KEY is not set, so a mailbox refresh token cannot be encrypted. Refusing to " +
        "write it in the clear.",
    );
  }
  if (plaintext.startsWith(PREFIX)) return plaintext;

  const [result] = await cipher.encrypt({ name: keyName, plaintext: Buffer.from(plaintext, "utf8") });
  if (!result.ciphertext) throw new Error("KMS returned no ciphertext for a token it was asked to encrypt.");
  return PREFIX + Buffer.from(result.ciphertext as Uint8Array).toString("base64");
}

export async function decryptSecret(
  stored: string | null | undefined,
  keyName: string | null,
  cipher: KmsCipher = getKmsClient(),
): Promise<string | null> {
  if (stored === null || stored === undefined || stored === "") return null;
  // A value written before this existed, or imported from the laptop's database. Readable, and
  // re-encrypted the next time it is saved.
  if (!stored.startsWith(PREFIX)) return stored;
  if (!keyName) {
    throw new Error("OUTREACH_KMS_KEY is not set, so an encrypted token cannot be read back.");
  }

  const [result] = await cipher.decrypt({
    ciphertext: Buffer.from(stored.slice(PREFIX.length), "base64"),
    name: keyName,
  });
  if (!result.plaintext) throw new Error("KMS returned no plaintext for a token it was asked to decrypt.");
  return Buffer.from(result.plaintext as Uint8Array).toString("utf8");
}

export function isEncrypted(value: string | null | undefined): boolean {
  return typeof value === "string" && value.startsWith(PREFIX);
}
