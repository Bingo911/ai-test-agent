/** Session context: who the caller is, what this deployment offers, and which project is in view. */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ApiFailure, api, setTenantHint, setToken, tenantHint, token } from "./api";
import type { Capabilities, Project, Whoami } from "./types";

type Session = {
  signedIn: boolean;
  signingIn: boolean;
  authError: string;
  whoami: Whoami | null;
  capabilities: Capabilities | null;
  projects: Project[];
  projectId: string;
  project: Project | null;
  permissions: string[];
  can: (permission: string) => boolean;
  signIn: (bearer: string, tenant: string) => Promise<boolean>;
  signOut: () => void;
  selectProject: (projectId: string) => void;
  reloadProjects: () => void;
};

const SessionContext = createContext<Session | null>(null);
const PROJECT_KEY = "aita.project";

/** The admin role carries every project permission server-side, so the console reads it from the same field. */
function permissionList(whoami: Whoami | null, projectId: string): string[] {
  if (!whoami) return [];
  const scoped = whoami.permissions_by_project?.[projectId];
  if (Array.isArray(scoped) && scoped.length > 0) return scoped;
  if (whoami.is_admin) return ["admin_users", "case_read", "case_write", "case_compile", "execution_run", "execution_cancel_any", "env_manage", "secret_manage", "quality_read", "audit_read", "human_control", "project_manage"];
  return [];
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [whoami, setWhoami] = useState<Whoami | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string>(localStorage.getItem(PROJECT_KEY) ?? "");
  const [authError, setAuthError] = useState("");
  const [signingIn, setSigningIn] = useState(false);

  const loadWorkspace = useCallback(async () => {
    const [who, caps, list] = await Promise.all([
      api.get<Whoami>("/whoami"),
      api.get<Capabilities>("/capabilities"),
      api.get<{ items: Project[] }>("/projects", { limit: 100 }),
    ]);
    setWhoami(who);
    setCapabilities(caps);
    setProjects(list.items);
    setProjectId((current) => {
      const wanted = localStorage.getItem(PROJECT_KEY) ?? "";
      const chosen = list.items.some((item) => item.id === current) ? current : list.items.some((item) => item.id === wanted) ? wanted : "";
      const next = chosen || list.items[0]?.id || "";
      localStorage.setItem(PROJECT_KEY, next);
      return next;
    });
    setAuthError("");
  }, []);

  useEffect(() => {
    if (!token()) return;
    loadWorkspace().catch((error: unknown) => {
      setAuthError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
      setWhoami(null);
    });
  }, [loadWorkspace]);

  const value = useMemo<Session>(() => {
    const permissions = permissionList(whoami, projectId);
    return {
      signedIn: Boolean(whoami),
      signingIn,
      authError,
      whoami,
      capabilities,
      projects,
      projectId,
      project: projects.find((item) => item.id === projectId) ?? null,
      permissions,
      can: (permission: string) => permissions.includes(permission),
      signIn: async (bearer: string, tenant: string) => {
        setSigningIn(true);
        setToken(bearer);
        setTenantHint(tenant.trim() || null);
        try {
          await loadWorkspace();
          return true;
        } catch (error) {
          setToken(null);
          setTenantHint(null);
          setAuthError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : "无法连接 API，请确认后端已启动。");
          setWhoami(null);
          return false;
        } finally {
          setSigningIn(false);
        }
      },
      signOut: () => {
        setToken(null);
        setTenantHint(null);
        setWhoami(null);
        setCapabilities(null);
        setProjects([]);
        setAuthError("");
      },
      selectProject: (next: string) => {
        setProjectId(next);
        localStorage.setItem(PROJECT_KEY, next);
      },
      reloadProjects: () => {
        api
          .get<{ items: Project[] }>("/projects", { limit: 100 })
          .then((list) => setProjects(list.items))
          .catch(() => undefined);
      },
    };
  }, [whoami, capabilities, projects, projectId, authError, signingIn, loadWorkspace]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const context = useContext(SessionContext);
  if (!context) throw new Error("SessionProvider is missing");
  return context;
}

export function currentTenantHint(): string {
  return tenantHint() ?? "";
}
