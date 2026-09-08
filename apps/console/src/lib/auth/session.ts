type ProviderSession = {
  subject?: string | null;
  user?: {
    email?: string | null;
    name?: string | null;
  } | null;
};

export function ownerIdFromSession(session: ProviderSession | null): string | null {
  const email = session?.user?.email?.trim().toLowerCase();
  if (email) {
    return email;
  }

  return session?.subject?.trim() || null;
}
