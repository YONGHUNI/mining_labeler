# =============================================================================
# Script Name: binned_trial_sampler.R
# Description: Takes the 90-row manifest as input and partitions it into 10 
#              distinct trials (9 samples per trial, 1 per stratum) using 
#              strict sampling without replacement.
# =============================================================================

# Enforce legacy RNG architecture to neutralize future R version drift bugs
RNGversion("4.5.3")
RANDOM_SEED <- 42
INPUT_CSV   <- "./sampled_scenes_manifest.csv"
OUTPUT_DIR  <- "./trials/"

# Create output directory for individual trials if it does not exist
if (!dir.exists(OUTPUT_DIR)) {
    dir.create(OUTPUT_DIR)
}

# Load the 90-row sampled manifest provided by the user
manifest_df <- read.csv(INPUT_CSV)

# Create distinct split chunks based on Stratum keys (Country + Category)
strata_keys  <- paste(manifest_df$COUNTRY_NAME, manifest_df$Mining_Category, sep = "::")
split_chunks <- split(manifest_df, strata_keys)

shuffled_chunks_list <- list()
set.seed(RANDOM_SEED) # Freeze the seed immediately prior to the shuffling block

for (i in seq_along(split_chunks)) {
    chunk      <- split_chunks[[i]]
    total_rows <- nrow(chunk)
    
    if (total_rows > 0) {
        # Shuffle all 10 rows within this stratum without replacement
        shuffled_indices <- sample(1:total_rows, size = total_rows, replace = FALSE)
        shuffled_chunk   <- chunk[shuffled_indices, ]
        
        # Assign a unique Trial_ID (1 to 10) to each row to enforce non-replacement
        shuffled_chunk$Trial_ID <- 1:total_rows
        
        shuffled_chunks_list[[length(shuffled_chunks_list) + 1]] <- shuffled_chunk
    }
}

# Consolidate all shuffled strata back into a unified data structure
partitioned_manifest <- do.call(rbind, shuffled_chunks_list)
rownames(partitioned_manifest) <- NULL

# -----------------------------------------------------------------------------
# EXPORT LAYER: Generate 10 separate clean CSV files and 1 master log
# -----------------------------------------------------------------------------
for (trial in 1:10) {
    # Extract exactly 9 rows (1 from each stratum) designated for this specific trial
    trial_df <- subset(partitioned_manifest, Trial_ID == trial)
    
    # Remove the temporary tracking column to keep the file format clean
    trial_df$Trial_ID <- NULL
    
    # Save individual trial file
    output_file <- sprintf("%strial_%d.csv", OUTPUT_DIR, trial)
    write.csv(trial_df, file = output_file, row.names = FALSE, na = "")
}

# Save a comprehensive master file with Trial_ID included for data traceability
write.csv(partitioned_manifest, file = "./partitioned_trials_master.csv", row.names = FALSE, na = "")

cat("\n=====================================================================\n")
cat("SUCCESS: Partitioned 90 rows into 10 distinct trials (9 rows each).\n")
cat("Individual clean files saved to: './trials/trial_1.csv' through 'trial_10.csv'\n")
cat("Master tracking log saved to   : './partitioned_trials_master.csv'\n")
cat("=====================================================================\n")