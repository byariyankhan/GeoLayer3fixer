# PyInstaller entry point.
# freeze_support() must run BEFORE the GUI module is imported: every worker
# process of the parallel fixer re-launches this exe, and importing the GUI
# (customtkinter, log file handle) in 32 workers wastes seconds and RAM.
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from geolayer_fixer.app import main
    main()
