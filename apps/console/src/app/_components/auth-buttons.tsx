import { signOut } from "@/auth";

/** Sign out. `compact` is the sidebar's form: an icon with its name as the label and tooltip. */
export function SignOutButton({ compact = false }: { compact?: boolean }) {
  return (
    <form
      action={async () => {
        "use server";
        await signOut({ redirectTo: "/sign-in" });
      }}
    >
      {compact ? (
        <button aria-label="Sign out" className="icon-btn" title="Sign out" type="submit">
          <svg aria-hidden="true" fill="none" focusable="false" stroke="currentColor"
            strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" viewBox="0 0 24 24">
            <path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4M14 16l4-4-4-4M18 12H9" />
          </svg>
        </button>
      ) : (
        <button className="chip" type="submit">
          Sign out
        </button>
      )}
    </form>
  );
}
