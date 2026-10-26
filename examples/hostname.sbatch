#!/usr/bin/env bash
#SBATCH --job-name=hostnames
#SBATCH --partition=debug
#SBATCH --nodes=5
#SBATCH --ntasks=5
#SBATCH --ntasks-per-node=1
#SBATCH --output=/shared/output/%x-%j.out

set -euo pipefail
srun /usr/bin/hostname | sort
