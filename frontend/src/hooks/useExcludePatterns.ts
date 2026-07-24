import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '../api/client';

export function useExcludePatterns() {
  return useQuery({
    queryKey: ['excludePatterns'],
    queryFn: api.getExcludePatterns,
  });
}

export function useCreateExcludePattern() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (pattern: string) => api.createExcludePattern(pattern),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['excludePatterns'] });
      qc.invalidateQueries({ queryKey: ['articles'] });
      qc.invalidateQueries({ queryKey: ['feeds'] });
    },
  });
}

export function useDeleteExcludePattern() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteExcludePattern(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['excludePatterns'] });
    },
  });
}
