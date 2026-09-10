import "next-auth";
import "next-auth/jwt";

declare module "next-auth" {
  interface Session {
    subject?: string | null;
    organizationId: string;
    emailVerified: boolean;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    organizationId?: string;
    emailVerified?: boolean;
  }
}
