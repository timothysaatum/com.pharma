import { useEffect, useRef } from "react";
import { toast } from "sonner";

const IS_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

function isDevMode(): boolean {
    try {
        return !import.meta.env.PROD;
    } catch {
        return true;
    }
}

export function UpdateManager() {
    const checkAttempted = useRef(false);

    useEffect(() => {
        // Only run update check in production Tauri builds
        if (!IS_TAURI || isDevMode()) return;

        if (checkAttempted.current) return;
        checkAttempted.current = true;

        const checkForUpdates = async () => {
            try {
                const { check } = await import(/* @vite-ignore */ "@tauri-apps/plugin-updater");
                const { relaunch } = await import(/* @vite-ignore */ "@tauri-apps/plugin-process");

                const update = await check();

                if (update) {
                    toast.info(`New version ${update.version} is available`, {
                        description: "An update is ready to be installed.",
                        duration: Infinity,
                        action: {
                            label: "Update and Restart",
                            onClick: async () => {
                                const id = toast.loading("Downloading and installing update...");
                                try {
                                    await update.downloadAndInstall();
                                    toast.success("Update installed successfully. Restarting...", { id });
                                    setTimeout(async () => {
                                        await relaunch();
                                    }, 1500);
                                } catch {
                                    toast.error("Failed to install update. Please try again later.", { id });
                                }
                            },
                        },
                    });
                }
            } catch {
                // Silently ignore — no public release channel configured for this project
            }
        };

        void checkForUpdates();
    }, []);

    return null;
}
