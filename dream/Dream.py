# -*- coding: utf-8 -*-
"""
Created on Wed Oct  1 17:21:29 2014

A Python version of MT-DREAM(ZS) that will run without PyMC.

@author: Erin
"""

import numpy as np
import random
import Dream_shared_vars
from parameters import UniformParam
from datetime import datetime
import traceback
import multiprocess as mp
import multiprocess.pool as mp_pool
import time

class Dream():
    """An implementation of the MT-DREAM(ZS) algorithm introduced in:
        Laloy, E. & Vrugt, J. A. High-dimensional posterior exploration of hydrologic models using multiple-try DREAM (ZS) and high-performance computing. Water Resources Research 48, W01526 (2012).
    
    Parameters
    ----------
    variables : PyMC variables
        Model parameters to be sampled.  
    nseedchains : int
        Number of draws with which to initialize the DREAM history.  Default = 10 * n dimensions
    nCR : int
        Number of crossover values to sample from during run (and to fit during crossover burn-in period).  Default = 3
    adapt_crossover : bool
        Whether to adapt crossover values during the burn-in period.  Default is to adapt.
    crossover_burnin : int
        Number of iterations to fit the crossover values.  Defaults to 10% of total iterations.
    DEpairs : int or list
        Number of chain pairs to use for crossover and selection of next point.  Default = 1.  Can pass a list to have a random number of pairs selected every iteration.
    lamb : float
        e sub d in DREAM papers.  Random error for ergodicity.  Default = .05
    zeta : float
        Epsilon in DREAM papers.  Randomization term. Default = 1e-12
    history_thin : int
        Thinning rate for history to reduce storage requirements.  Every n-th iteration will be added to the history.
    snooker : float
        Probability of proposing a snooker update.  Default is .1.  To forego snooker updates, set to 0.
    p_gamma_unity : float
        Probability of proposing a point with gamma=unity (i.e. a point relatively far from the current point to enable jumping between disconnected modes).  Default = .2.
    start_random : bool
        Whether to intialize chains from a random point in parameter space drawn from the prior (default = yes).  Will override starting position set when sample was called, if any.
    save_history : bool
        Whether to save the history to file at the end of the run (essential if you want to continue the run).  Default is yes.
    history_file : str
        Name of history file to be loaded.  Assumed to be in directory you ran the script from.  If False, no file to be loaded.
    crossover_file : str
        Name of crossover file to be loaded. Assumed to be in directory you ran the script from.  If False, no file to be loaded.
    multitry : bool
        Whether to utilize multi-try sampling.  Default is no.  If set to True, will be set to 5 multiple tries.  Can also directly specify an integer if desired.
    parallel : bool
        Whether to run multi-try samples in parallel (using multiprocessing).  Default is false.  Irrelevant if multitry is set to False.
    verbose : bool
        Whether to print verbose progress.  Default is false.
    model_name : str
        A model name to be used as a prefix when saving history and crossover value files.
    """
    
    def __init__(self, model, variables=None, nseedchains=None, nCR=3, adapt_crossover=True, adapt_gamma=False, crossover_burnin=None, DEpairs=1, lamb=.05, zeta=1e-12, history_thin=10, snooker=.10, p_gamma_unity=.20, gamma_levels=1, start_random=True, save_history=True, history_file=False, crossover_file=False, gamma_file=False, multitry=False, parallel=False, verbose=False, model_name=False, **kwargs):
        
        self.model = model
        self.model_name = model_name
        if variables == None:
            self.variables = model.sampled_parameters
        self.variables = variables
        self.nseedchains = nseedchains
        self.nCR = nCR
        self.ngamma = gamma_levels
        self.njoint_cr_gamma_probs = nCR*gamma_levels
        self.crossover_burnin = crossover_burnin
        self.crossover_file = crossover_file
        if crossover_file:
            self.CR_probabilities = np.load(crossover_file)
            if adapt_crossover:
                print('Warning: Crossover values loaded but adapt_crossover = True.  Overrode adapt_crossover input and not adapting crossover values.')
                self.adapt_crossover = False
        else:
            self.CR_probabilities = [1/float(self.nCR) for i in range(self.nCR)]
            self.adapt_crossover = adapt_crossover
        
        if gamma_file:
            self.gamma_probabilities = np.load(gamma_file)
            if adapt_gamma:
                print('Warning: Gamma values loaded but adapt gamma = True.  Overrode adapt_gamma input and not adapting gamma values.')
                self.adapt_gamma = False
        else:
            self.gamma_probabilities = [1/float(self.ngamma) for i in range(self.ngamma)]
            self.adapt_gamma = adapt_gamma
            
            
        self.CR_values = np.array([m/float(self.nCR) for m in range(1, self.nCR+1)])  
        self.gamma_level_values = np.array([m for m in range(1, self.ngamma+1)])
        self.DEpairs = np.linspace(1, DEpairs, num=DEpairs) #This is delta in original Matlab code
        self.snooker = snooker
        self.p_gamma_unity = p_gamma_unity 
        if multitry == False:
            self.multitry = 1
        elif multitry == True:
            self.multitry = 5
        else:
            self.multitry = multitry
        self.parallel = parallel
        self.lamb = lamb #This is e sub d in DREAM papers
        self.zeta = zeta #This is epsilon in DREAM papers
        self.last_logp = None
        self.boundaries = False
        self.total_var_dimension = 0
        for var in variables:
            self.total_var_dimension += var.dsize
            if isinstance(var, UniformParam):
              self.boundaries = True
        
        if self.boundaries:
            self.boundary_mask = np.zeros((self.total_var_dimension), dtype=bool)
            self.mins = []
            self.maxs = []
            n = 0
            for var in variables:
                if isinstance(var, UniformParam):
                    self.boundary_mask[n:n+var.dsize] = True
                    self.mins.append(var.lower)
                    self.maxs.append(var.upper)
                n += var.dsize

            self.mins = np.concatenate([vals for vals in self.mins])
            self.maxs = np.concatenate([vals for vals in self.maxs])
            #self.maxs = np.ravel(self.maxs)
        if self.nseedchains == None:
            self.nseedchains = self.total_var_dimension*10
        gamma_array = np.zeros((self.ngamma, DEpairs, self.total_var_dimension))
        gamma_level_decrease = 1
        for gamma_level in range(1, self.ngamma+1):         
            for delta in range(1, DEpairs+1):
                gamma_array[gamma_level-1, delta-1, :] = (2.38 / np.sqrt(2*delta*np.linspace(1, self.total_var_dimension, num=self.total_var_dimension)))/gamma_level_decrease
            gamma_level_decrease = gamma_level_decrease*2
        self.gamma_arr = np.squeeze(gamma_array)
        self.gamma = None
        self.iter = 0  
        self.chain_n = None
        self.nchains = None
        self.len_history = 0
        self.save_history = save_history
        self.history_file = history_file
        self.history_thin = history_thin
        self.start_random = start_random
        self.verbose = verbose
        self.logp = self.model.total_logp
    
    def astep(self, q0, T=1., last_loglike=None, last_logprior=None):
        # On first iteration, check that shared variables have been initialized (which only occurs if multiple chains have been started).
        if self.iter == 0:   
 
            try:
                with Dream_shared_vars.nchains.get_lock():
                    self.chain_n = Dream_shared_vars.nchains.value-1
                    Dream_shared_vars.nchains.value -= 1

                # Assuming the shared variables exist, seed the history with nseedchain draws from the prior
                with Dream_shared_vars.history_seeded.get_lock() and Dream_shared_vars.history.get_lock():
                    if not self.history_file:
                        if self.verbose:
                            print('History file not loaded.')
                        if Dream_shared_vars.history_seeded.value == 'F':
                            if self.verbose:
                                print('Seeding history with ',self.nseedchains,' draws from prior.')
                            for i in range(self.nseedchains):
                                start_loc = i*self.total_var_dimension
                                end_loc = start_loc+self.total_var_dimension
                                Dream_shared_vars.history[start_loc:end_loc] = self.draw_from_prior(self.variables)
                            Dream_shared_vars.history_seeded.value = 'T'
                    else:
                        if self.verbose:
                            print('History file loaded.')
                    if self.verbose:
                        print('Setting crossover probability starting values.')
                        print('Set probability of different crossover values to: ',self.CR_probabilities)
                    if self.start_random:
                        if self.verbose:
                            print('Setting start to random draw from prior.')
                        q0 = self.draw_from_prior(self.variables)
                    if self.verbose:
                        print('Start: ',q0)
                # Also get length of history array so we know when to save it at end of run.
                if self.save_history:
                    with Dream_shared_vars.history.get_lock():
                        self.len_history = len(np.frombuffer(Dream_shared_vars.history.get_obj()))
            
            except AttributeError:
                raise Exception('Dream should be run with multiple chains in parallel.  Set nchains > 1.')          
        
        try:
            if last_loglike != None:
                self.last_like = last_loglike
                self.last_prior = last_logprior
                self.last_logp = T*self.last_like + self.last_prior
                
            #Determine whether to run snooker update or not for this iteration.
            run_snooker = self.set_snooker()
            
    
            #Set crossover value for generating proposal point
            CR = self.set_CR(self.CR_probabilities, self.CR_values)
            
            #Set DE pair choice to be used for generating proposal point for this iteration.
            DEpair_choice = self.set_DEpair(self.DEpairs)
            
            #Select gamma size level
            gamma_level = self.set_gamma_level(self.gamma_probabilities, self.gamma_level_values)
        
            with Dream_shared_vars.history.get_lock() and Dream_shared_vars.count.get_lock():
                #Generate proposal points
                if not run_snooker:
                    proposed_pts = self.generate_proposal_points(self.multitry, q0, CR, DEpair_choice, gamma_level, snooker=False)
                    
                else:
                    proposed_pts, snooker_logp_prop, z = self.generate_proposal_points(self.multitry, q0, CR, DEpair_choice, gamma_level, snooker=True)                 
                    
            if self.last_logp == None:
                self.last_prior, self.last_like = self.logp(q0)
                self.last_logp = T*self.last_like + self.last_prior
            
            #Evaluate logp(s)
            if self.multitry == 1:
                q_prior, q_loglike_noT = self.logp(np.squeeze(proposed_pts)) 
                q_logp_noT = q_prior + q_loglike_noT
                q_logp = T*q_loglike_noT + q_prior
                q = np.squeeze(proposed_pts)
            else:
                
                log_priors, log_likes = self.mt_evaluate_logps(self.parallel, self.multitry, proposed_pts, self.logp, ref=False)
                log_ps = T*log_likes + log_priors
                    
                #Check if all logps are -inf, in which case they'll all be impossible and we need to generate more proposal points
                while np.all(np.isfinite(np.array(log_ps))==False):
                    if run_snooker:
                        proposed_pts, snooker_logp_prop, z = self.generate_proposal_points(self.multitry, q0, CR, DEpair_choice, gamma_level, snooker=run_snooker)
                    else:
                        proposed_pts = self.generate_proposal_points(self.multitry, q0, CR, DEpair_choice, gamma_level, snooker=run_snooker)
                        
                    log_priors, log_likes = self.mt_evaluate_logps(self.parallel, self.multitry, proposed_pts, self.logp, ref=False)
                    log_ps = T*log_likes + log_priors
                    
                q_proposal, q_logp, q_logp_noT, q_loglike_noT, q_prior = self.mt_choose_proposal_pt(log_priors, log_likes, proposed_pts, T)
                
            
                #Draw reference points around the randomly selected proposal point
                with Dream_shared_vars.history.get_lock() and Dream_shared_vars.count.get_lock():
                    if run_snooker:
                        reference_pts, snooker_logp_ref, z_ref = self.generate_proposal_points(self.multitry-1, q_proposal, CR, DEpair_choice, gamma_level, snooker=run_snooker)
                    else:
                        reference_pts = self.generate_proposal_points(self.multitry-1, q_proposal, CR, DEpair_choice, gamma_level, snooker=run_snooker)
                    
                #Compute posterior density at reference points.
                ref_log_priors, ref_log_likes = self.mt_evaluate_logps(self.parallel, self.multitry, reference_pts, self.logp, ref=True)
                ref_log_ps = T*ref_log_likes + ref_log_priors
        
            if self.multitry > 1:
                if run_snooker:
                    total_proposal_logp = log_ps + snooker_logp_prop
                    #Goal is to determine the ratio =  p(y) * p(y --> X) / p(Xref) * p(Xref --> X) where y = proposal point, X = current point, and Xref = reference point
                    # First determine p(y --> X) (i.e. moving from proposed point y to original point X)
                    # p(y --> X) equals ||y - z||^(n-1), i.e. the snooker_logp for the proposed point
                    # p(Xref --> X) is equal to p(Xref --> y) * p(y --> X) (i.e. moving from Xref to proposed point y to original point X)
                    snooker_logp_ref = np.append(snooker_logp_ref, 0)
                    total_reference_logp = ref_log_ps + snooker_logp_ref + snooker_logp_prop
                
                else:
                    total_proposal_logp = log_ps
                    total_reference_logp = ref_log_ps
                
                #Determine max logp for all proposed and reference points
                max_logp = np.amax(np.concatenate((total_proposal_logp, total_reference_logp)))
                weight_proposed = np.exp(total_proposal_logp - max_logp)
                weight_reference = np.exp(total_reference_logp - max_logp)
                q_new = metrop_select(np.nan_to_num(np.log(np.sum(weight_proposed)/np.sum(weight_reference))), q_proposal, q0)
                
            else:  
                if run_snooker:
                    total_proposed_logp = q_logp + snooker_logp_prop
                    snooker_current_logp = np.log(np.linalg.norm(q0-z))*(self.total_var_dimension-1)
                    total_old_logp = self.last_logp + snooker_current_logp
                    
                    q_new = metrop_select(np.nan_to_num(total_proposed_logp - total_old_logp, q, q0))
                else:
                    q_new = metrop_select(np.nan_to_num(np.nan_to_num(q_logp) - np.nan_to_num(self.last_logp), q, q0))
                    
            if not np.array_equal(q0, q_new):
                if self.multitry==1:
                    print('Accepted point.  New logp: ',q_logp,' old logp: ',self.last_logp, ' at temperature: ',T)
                    
                else: 
                    print('Accepted point.  New logp: ',q_logp,' old logp: ',self.last_logp,' weight proposed: ',log_ps,' weight ref: ',ref_log_ps,' ratio: ',np.sum(weight_proposed)/np.sum(weight_reference),' at temperature: ',T)
                    
                self.last_logp = q_logp_noT
                self.last_prior = q_prior
                self.last_like = q_loglike_noT
                
            else:
                if self.multitry==1:
                    print('Did not accept point.  Kept old logp: ',self.last_logp,' Tested logp: ',q_logp,' at temperature: ',T)
                
                else:
                    print('Did not accept point.  Kept old logp: ',self.last_logp,' Tested logp: ',q_logp,' weight proposed: ',log_ps,' weight ref: ',ref_log_ps,' ratio: ',np.sum(weight_proposed)/np.sum(weight_reference),' at temperature: ',T)
                
        
            #Place new point in history given history thinning rate
            if self.iter % self.history_thin == 0:
                with Dream_shared_vars.history.get_lock() and Dream_shared_vars.count.get_lock():
                    self.record_history(self.nseedchains, self.total_var_dimension, q_new, self.len_history)
            
            if self.iter < self.crossover_burnin+1:
                with Dream_shared_vars.current_positions.get_lock():
                    self.set_current_position_arr(self.total_var_dimension, q_new)
                    
            #If adapting crossover values, estimate ideal crossover probabilities for each dimension during burn-in.
            #Don't do this for the first 10 iterations to give all chains a chance to fill in the shared current position array
            #Don't count iterations where gamma was set to 1 in crossover adaptation calculations
            if self.adapt_crossover and self.iter > 10 and self.iter < self.crossover_burnin and not np.any(np.array(self.gamma)==1.0):
                with Dream_shared_vars.cross_probs.get_lock() and Dream_shared_vars.count.get_lock() and Dream_shared_vars.ncr_updates.get_lock() and Dream_shared_vars.current_positions.get_lock() and Dream_shared_vars.delta_m.get_lock():
                    #If a snooker update was run, then regardless of the originally selected CR, a CR=1.0 was used.
                    if not run_snooker:
                        self.CR_probabilities = self.estimate_crossover_probabilities(self.total_var_dimension, q0, q_new, CR) 
                    else:
                        self.CR_probabilities = self.estimate_crossover_probabilities(self.total_var_dimension, q0, q_new, CR=1)
            
            if self.adapt_gamma and self.iter > 10 and self.iter < self.crossover_burnin and not np.any(np.array(self.gamma)==1.0) and not run_snooker:
               with Dream_shared_vars.gamma_level_probs.get_lock() and Dream_shared_vars.count.get_lock() and Dream_shared_vars.ngamma_updates.get_lock() and Dream_shared_vars.current_positions.get_lock() and Dream_shared_vars.delta_m_gamma.get_lock():
                   self.gamma_probabilities = self.estimate_gamma_level_probs(self.total_var_dimension, q0, q_new, gamma_level)
            
            if self.iter == self.crossover_burnin:
                #To ensure all chains use the same fitted shared probability values, wait for all parallel chains to reach end of burnin period before grabbing shared probabilities
                with Dream_shared_vars.nchains.get_lock():
                    Dream_shared_vars.nchains.value += 1 
                    nchains_finished_burnin = Dream_shared_vars.nchains.value
                
                if self.adapt_gamma:
                    with Dream_shared_vars.gamma_level_probs.get_lock() and Dream_shared_vars.count.get_lock() and Dream_shared_vars.ngamma_updates.get_lock() and Dream_shared_vars.current_positions.get_lock() and Dream_shared_vars.delta_m_gamma.get_lock():
                        self.gamma_probabilities = self.estimate_gamma_level_probs(self.total_var_dimension, q0, q_new, gamma_level)
                
                if self.adapt_crossover:
                    with Dream_shared_vars.cross_probs.get_lock() and Dream_shared_vars.count.get_lock() and Dream_shared_vars.ncr_updates.get_lock() and Dream_shared_vars.current_positions.get_lock() and Dream_shared_vars.delta_m.get_lock():
                        #If a snooker update was run, then regardless of the originally selected CR, a CR=1.0 was used.
                        if not run_snooker:
                            self.CR_probabilities = self.estimate_crossover_probabilities(self.total_var_dimension, q0, q_new, CR) 
                        else:
                            self.CR_probabilities = self.estimate_crossover_probabilities(self.total_var_dimension, q0, q_new, CR=1)
                
                while nchains_finished_burnin != self.nchains:
                    time.sleep(30)
                    with Dream_shared_vars.nchains.get_lock():
                        nchains_finished_burnin = Dream_shared_vars.nchains.value    
                time.sleep(10)
                
                if self.adapt_gamma:
                    with Dream_shared_vars.gamma_level_probs.get_lock():
                        self.gamma_probabilities = Dream_shared_vars.gamma_level_probs[0:self.ngamma]
                
                if self.adapt_crossover:
                    with Dream_shared_vars.cross_probs.get_lock():
                        self.CR_probabilities = Dream_shared_vars.cross_probs[0:self.nCR]
                               
                    print 'CR probs: ',self.CR_probabilities,' and gamma probs: ',self.gamma_probabilities,' in chain: ',self.chain_n
            
            self.iter += 1
        except Exception as e:
            traceback.print_exc()
            print()
            raise e
        return q_new, self.last_prior, self.last_like
        
    def set_current_position_arr(self, ndimensions, q_new):
        """Add current position of chain to shared array available to other chains."""
        
        if self.nchains == None:
           current_positions = np.frombuffer(Dream_shared_vars.current_positions.get_obj()) 
           self.nchains = len(current_positions)/ndimensions  
        
        if self.chain_n == None:
            with Dream_shared_vars.nchains.get_lock():
                self.chain_n = Dream_shared_vars.nchains.value-1
                Dream_shared_vars.nchains.value -= 1
        
        #We only need to have the current position of all chains for estimating the crossover probabilities during burn-in so don't bother updating after that
        if self.iter < self.crossover_burnin+1:
            start_cp = self.chain_n*ndimensions
            end_cp = start_cp+ndimensions
            Dream_shared_vars.current_positions[start_cp:end_cp] = np.array(q_new).flatten()        
        
    def estimate_crossover_probabilities(self, ndim, q0, q_new, CR):
        """Adapt crossover probabilities during crossover burn-in period."""
            
        cross_probs = Dream_shared_vars.cross_probs[0:self.nCR]   
        
        #Compute squared normalized jumping distance
        m_loc = np.where(self.CR_values == CR)[0]

        Dream_shared_vars.ncr_updates[m_loc] += 1   
        
        current_positions = np.frombuffer(Dream_shared_vars.current_positions.get_obj())
        

        current_positions = current_positions.reshape((self.nchains, ndim))
        
        sd_by_dim = np.std(current_positions, axis=0)
        
        
        
        Dream_shared_vars.delta_m[m_loc] = Dream_shared_vars.delta_m[m_loc] + np.nan_to_num(np.sum(((q_new - q0)/sd_by_dim)**2))
        
        #Update probabilities of tested crossover value        
        #Leave probabilities unchanged until all possible crossover values have had at least one successful move so that a given value's probability isn't prematurely set to 0, preventing further testing.
        delta_ms = np.array(Dream_shared_vars.delta_m[0:self.nCR])
        
        if np.all(delta_ms != 0) == True:

            for m in range(self.nCR):
                cross_probs[m] = (Dream_shared_vars.delta_m[m]/Dream_shared_vars.ncr_updates[m])*self.nchains
            cross_probs = cross_probs/np.sum(cross_probs)
        
        Dream_shared_vars.cross_probs[0:self.nCR] = cross_probs
        
        self.CR_probabilities = cross_probs
        
        return cross_probs
    
    def estimate_gamma_level_probs(self, ndim, q0, q_new, gamma_level):
        current_positions = np.frombuffer(Dream_shared_vars.current_positions.get_obj())

        current_positions = current_positions.reshape((self.nchains, ndim))

        sd_by_dim = np.std(current_positions, axis=0)
        
        gamma_level_probs = Dream_shared_vars.gamma_level_probs[0:self.ngamma]
            
        gamma_loc = np.where(self.gamma_level_values == gamma_level)[0]
            
        Dream_shared_vars.ngamma_updates[gamma_loc] += 1
            
        Dream_shared_vars.delta_m_gamma[gamma_loc] = Dream_shared_vars.delta_m_gamma[gamma_loc] + np.nan_to_num(np.sum(((q_new - q0)/sd_by_dim)**2))
    
        delta_ms_gamma = np.array(Dream_shared_vars.delta_m_gamma[0:self.ngamma])
            
        if np.all(delta_ms_gamma != 0) == True:
                
            for m in range(self.ngamma):
                gamma_level_probs[m] = (Dream_shared_vars.delta_m_gamma[m]/Dream_shared_vars.ngamma_updates[m])*self.nchains
                
            gamma_level_probs = gamma_level_probs/np.sum(gamma_level_probs)
            
        Dream_shared_vars.gamma_level_probs[0:self.ngamma] = gamma_level_probs 
        
        return gamma_level_probs
    
    def set_snooker(self):
        """Choose to run a snooker update on a given iteration or not."""
        if self.snooker != 0:
            snooker_choice = np.where(np.random.multinomial(1, [self.snooker, 1-self.snooker])==1)
                
            if snooker_choice[0] == 0:
                run_snooker = True
            else:
                run_snooker = False
        else:
            run_snooker = False
            
        return run_snooker
    
    def set_CR(self, CR_probs, CR_vals):
        """Select crossover value for a given iteration."""
        CR_loc = np.where(np.random.multinomial(1, CR_probs)==1)

        CR = CR_vals[CR_loc]
        
        return CR
    
    def set_DEpair(self, DEpairs):
        """Select the number of pairs of chains to be used for creating the next proposal point for a given iteration."""
        if len(DEpairs)>1:
            DEpair_choice = np.squeeze(np.random.randint(1, len(DEpairs)+1, size=1))
        else:
            DEpair_choice = 1
        return DEpair_choice
    
    def set_gamma_level(self, gamma_level_probs, gamma_level_vals):
        gamma_loc = np.where(np.random.multinomial(1, gamma_level_probs)==1)
        
        gamma_level = gamma_level_vals[gamma_loc]
        
        return gamma_level
    
    def set_gamma(self, iteration, DEpairs, snooker_choice, gamma_level_choice, d_prime):
        """Select gamma value for a given iteration."""
        
        gamma_unity_choice = np.where(np.random.multinomial(1, [self.p_gamma_unity, 1-self.p_gamma_unity])==1)
        
        if snooker_choice:
            gamma = np.random.uniform(1.2, 2.2)
            
        elif gamma_unity_choice[0] == 0:
            gamma = 1.0
        
        else:
            if self.ngamma == 1:
                if self.DEpairs == 1:
                    gamma = self.gamma_arr[d_prime-1]
                else:
                    gamma = self.gamma_arr[DEpairs-1][d_prime-1]
            else:
                gamma = self.gamma_arr[gamma_level_choice-1][DEpairs-1][d_prime-1]
            
        return gamma

    def draw_from_prior(self, model_vars):
        """Draw from a parameter's prior to seed history array."""
        
        draw = np.array([])
        for variable in model_vars:
            try:
                var_draw = variable.random()
            except AttributeError:
                raise Exception('Random draw from distribution for variable %s not implemented yet.' % variable)
            draw = np.append(draw, var_draw)
        return draw.flatten()

    def sample_from_history(self, nseedchains, DEpairs, ndimensions, snooker=False):
        """Draw random point from the history array."""
        if not snooker:
            chain_num = random.sample(range(Dream_shared_vars.count.value+nseedchains), DEpairs*2)
        else:
            chain_num = random.sample(range(Dream_shared_vars.count.value+nseedchains), 1)
        start_locs = [i*ndimensions for i in chain_num]
        end_locs = [i+ndimensions for i in start_locs]
        sampled_chains = [Dream_shared_vars.history[start_loc:end_loc] for start_loc, end_loc in zip(start_locs, end_locs)]
        return sampled_chains
        
    def generate_proposal_points(self, n_proposed_pts, q0, CR, DEpairs, gamma_level, snooker):
        """Generate proposal points."""

        if not snooker:
            
            sampled_history_pts = np.array([self.sample_from_history(self.nseedchains, DEpairs, self.total_var_dimension) for i in range(n_proposed_pts)])

            chain_differences = np.array([np.sum(sampled_history_pts[i][0:DEpairs], axis=0)-np.sum(sampled_history_pts[i][DEpairs:DEpairs*2], axis=0) for i in range(len(sampled_history_pts))])

            zeta = np.array([np.random.normal(0, self.zeta, self.total_var_dimension) for i in range(n_proposed_pts)])

            e = np.array([np.random.uniform(-self.lamb, self.lamb, self.total_var_dimension) for i in range(n_proposed_pts)])
            e = e+1

            d_prime = self.total_var_dimension
            U = np.random.uniform(0, 1, size=chain_differences.shape)
            
            #Select gamma values given number of parameter dimensions to be changed (d_prime).
            if n_proposed_pts > 1:
                d_prime = [len(U[point][np.where(U[point]<CR)]) for point in range(n_proposed_pts)]
                self.gamma = [self.set_gamma(self.iter, DEpairs, snooker, gamma_level, d_p) for d_p in d_prime]

                
            else:
                d_prime = len(U[np.where(U<CR)])
                self.gamma = self.set_gamma(self.iter, DEpairs, snooker, gamma_level, d_prime)
                
            #Generate proposed points given gamma values.
            if n_proposed_pts > 1:
                proposed_pts = [q0 + e[point]*gamma*chain_differences[point] + zeta[point] for point, gamma in zip(range(n_proposed_pts), self.gamma)]

            else:
                proposed_pts = q0+ e*self.gamma*chain_differences + zeta             
                
            #Crossover proposed points based on number of parameter dimensions to be changed.
            if np.any(d_prime != self.total_var_dimension):
                if n_proposed_pts > 1:
                    for point, pt_num in zip(proposed_pts, range(n_proposed_pts)):
                        proposed_pts[pt_num][np.where(U[pt_num]>CR)] = q0[np.where(U[pt_num]>CR)]

                else:
                    proposed_pts[np.where(U>CR)] = q0[np.where(U>CR)[1]] 

        else:
            #With a snooker update all CR always equals 1 (i.e. all parameter dimensions are changed).
            self.gamma = self.set_gamma(self.iter, DEpairs, snooker, gamma_level, self.total_var_dimension)
            proposed_pts, snooker_logp, z = self.snooker_update(n_proposed_pts, q0)

        #If uniform priors were used, check that proposed points are within bounds and reflect if not.
        if self.boundaries:
           if n_proposed_pts > 1:
               for pt_num in range(n_proposed_pts):
                   masked_point = proposed_pts[pt_num][self.boundary_mask]
                   x_lower = masked_point < self.mins
                   x_upper = masked_point > self.maxs
                   masked_point[x_lower] = 2 * self.mins[x_lower] - masked_point[x_lower]
                   masked_point[x_upper] = 2 * self.maxs[x_upper] - masked_point[x_upper]
                   
                   #Occasionally reflection will result in points still outside of boundaries
                   x_lower = masked_point < self.mins
                   x_upper = masked_point > self.maxs
                   masked_point[x_lower] = self.mins[x_lower] + np.random.rand(len(np.where(x_lower==True)[0])) * (self.maxs[x_lower]-self.mins[x_lower])
                   masked_point[x_upper] = self.mins[x_upper] + np.random.rand(len(np.where(x_upper==True)[0])) * (self.maxs[x_upper]-self.mins[x_upper])
                   proposed_pts[pt_num][self.boundary_mask] = masked_point
                   
           else:
               masked_point = np.squeeze(proposed_pts)[self.boundary_mask]
               x_lower = masked_point < self.mins
               x_upper = masked_point > self.maxs
               masked_point[x_lower] = 2 * self.mins[x_lower] - masked_point[x_lower]
               masked_point[x_upper] = 2 * self.maxs[x_upper] - masked_point[x_upper]
               
               #Occasionally reflection will result in points still outside of boundaries
               x_lower = masked_point < self.mins
               x_upper = masked_point > self.maxs
               masked_point[x_lower] = self.mins[x_lower] + np.random.rand(len(np.where(x_lower==True)[0])) * (self.maxs[x_lower]-self.mins[x_lower])
               masked_point[x_upper] = self.mins[x_upper] + np.random.rand(len(np.where(x_upper==True)[0])) * (self.maxs[x_upper]-self.mins[x_upper])
               if not snooker:
                   proposed_pts[0][self.boundary_mask] = masked_point
               else:
                   proposed_pts[self.boundary_mask] = masked_point

        if not snooker:
            return proposed_pts
        else:
            return proposed_pts, snooker_logp, z
        
    def snooker_update(self, n_proposed_pts, q0):
        """Generate a proposed point with snooker updating scheme."""
        
        sampled_history_pt = [self.sample_from_history(self.nseedchains, self.DEpairs, self.total_var_dimension, snooker=True) for i in range(n_proposed_pts)]

        chains_to_be_projected = np.squeeze([np.array([self.sample_from_history(self.nseedchains, self.DEpairs, self.total_var_dimension, snooker=True) for i in range(2)]) for x in range(n_proposed_pts)])

        #Define projection vector
        proj_vec_diff = np.squeeze(q0-sampled_history_pt)

        if n_proposed_pts > 1:
            D = [np.dot(proj_vec_diff[point], proj_vec_diff[point]) for point in range(len(proj_vec_diff))]
            
            #Orthogonal projection of chains_to_projected onto projection vector
            diff_chains_to_be_projected = [(chains_to_be_projected[point][0]-chains_to_be_projected[point][1]) for point in range(n_proposed_pts)]       
            zP = np.nan_to_num(np.array([np.sum(diff_chains_to_be_projected[point]*proj_vec_diff[point])/D[point] for point in range(n_proposed_pts)]))          
            dx = self.gamma*zP
            proposed_pts = [q0 + dx[point] for point in range(n_proposed_pts)]
            snooker_logp = [np.log(np.linalg.norm(proposed_pts[point]-sampled_history_pt[point]))*(self.total_var_dimension-1) for point in range(n_proposed_pts)]
        else:
            D = np.dot(proj_vec_diff, proj_vec_diff)

            #Orthogonal projection of chains_to_projected onto projection vector  
            diff_chains_to_be_projected = chains_to_be_projected[0]-chains_to_be_projected[1]
            zP = np.nan_to_num(np.array([np.sum(diff_chains_to_be_projected*proj_vec_diff)/D]))
            dx = self.gamma*zP
            proposed_pts = q0 + dx
            snooker_logp = np.log(np.linalg.norm(proposed_pts-sampled_history_pt))*(self.total_var_dimension-1)
        
        return proposed_pts, snooker_logp, sampled_history_pt
    
    def mt_evaluate_logps(self, parallel, multitry, proposed_pts, pfunc, ref=False):
        """Evaluate the log probability for multiple points in serial or parallel when using multi-try."""
        
        #If using multi-try and running in parallel farm out proposed points to process pool.
        if parallel:
            p = mp.Pool(multitry)
            args = zip([self]*multitry, np.squeeze(proposed_pts))
            log_ps = p.map(call_logp, args)
            p.close()
            p.join()
            
        else:
            log_priors = []
            log_likes = []
            if multitry == 2:
                log_priors, log_likes = np.array([pfunc(np.squeeze(proposed_pts))])
            else:
                for pt in np.squeeze(proposed_pts):
                    log_priors.append(pfunc(pt)[0])  
                    log_likes.append(pfunc(pt)[1])
        
        log_priors = np.array(log_priors)  
        log_likes = np.array(log_likes)
        
        if ref:
            log_likes = np.append(log_likes, self.last_like)
            log_priors = np.append(log_priors, self.last_prior)
            
        return log_priors, log_likes

    def mt_choose_proposal_pt(self, log_priors, log_likes, proposed_pts, T):
        """Select a proposed point with probability proportional to the probability density at that point."""
        
        #Substract largest logp from all logps (this from original Matlab code)
        org_log_likes = log_likes
        log_likes = T * log_likes
        log_ps = log_priors + log_likes
        noT_logps = org_log_likes + log_priors
        max_logp = np.amax(log_ps)
        log_ps_sub = np.exp(log_ps - max_logp)

        #Calculate probabilities
        sum_proposal_logps = np.sum(log_ps_sub)
        logp_prob = log_ps_sub/sum_proposal_logps
        best_logp_loc = np.where(np.random.multinomial(1, logp_prob)==1)[0]

        #Randomly select one of the tested points with probability proportional to the probability density at the point
        q_proposal = np.squeeze(proposed_pts[best_logp_loc])
        q_logp = log_ps[best_logp_loc] 
        q_prior = log_priors[best_logp_loc]
        noT_loglike = org_log_likes[best_logp_loc]
        noT_logp = noT_logps[best_logp_loc]
        
        return q_proposal, q_logp, noT_logp, noT_loglike, q_prior
        
    def record_history(self, nseedchains, ndimensions, q_new, len_history):
        """Record accepted point in history."""
        nhistoryrecs = Dream_shared_vars.count.value+nseedchains
        start_loc = nhistoryrecs*ndimensions
        end_loc = start_loc+ndimensions
        Dream_shared_vars.history[start_loc:end_loc] = np.array(q_new).flatten()      

        Dream_shared_vars.count.value += 1
        if self.save_history and len_history == (nhistoryrecs+1)*ndimensions:
            if not self.model_name:
                prefix = datetime.now().strftime('%Y_%m_%d_%H:%M:%S')+'_'
            else:
                prefix = self.model_name+'_'

            self.save_history_to_disc(np.frombuffer(Dream_shared_vars.history.get_obj()), prefix)
            
    def save_history_to_disc(self, history, prefix):
        """Save history and crossover probabilities to files at end of run."""
        
        filename = prefix+'DREAM_chain_history.npy'
        print('Saving history to file: ',filename)
        np.save(filename, history)
        
        #Also save crossover probabilities if adapted
        filename = prefix+'DREAM_chain_adapted_crossoverprob.npy'
        print('Saving fitted crossover values: ',self.CR_probabilities,' to file: ',filename)
        np.save(filename, self.CR_probabilities)
        
        #Also save gamma level probabilities
        filename = prefix+'DREAM_chain_adapted_gammalevelprob.npy'
        print('Saving fitted gamma level values: ',self.gamma_probabilities,' to file: ',filename)
        np.save(filename, self.gamma_probabilities)
    
def call_logp(args):
    #Defined at top level so it can be pickled.
    instance = args[0]
    tested_point = args[1]
    
    logp_fxn = getattr(instance, 'logp')
    
    return logp_fxn(tested_point)
    
def metrop_select(mr, q, q0):
    # Perform rejection/acceptance step for Metropolis class samplers

    # Compare acceptance ratio to uniform random number
    if np.isfinite(mr) and np.log(np.random.uniform()) < mr:
        # Accept proposed value
        return q
    else:
        # Reject proposed value
        return q0

        
class NoDaemonProcess(mp.Process):
    # make 'daemon' attribute always return False
    def _get_daemon(self):
        return False
    def _set_daemon(self, value):
        pass
    daemon = property(_get_daemon, _set_daemon)

#A subclass of multiprocessing.pool.Pool that allows processes to launch child processes (this is necessary for Dream to use multi-try)
#Taken from http://stackoverflow.com/questions/6974695/python-process-pool-non-daemonic
class DreamPool(mp_pool.Pool):
    Process = NoDaemonProcess
        