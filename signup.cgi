#!/home/biketempe/bin/perl
#!/usr/local/bin/perl

# XXX filter HTML from emails to the bikecount mail box?
# XXX send email to the person who volunteers when they sign up for an intersection
# XXX fix file locking
# XXX when sending the "someone signed up" email to the management address, use the email address if a name isn't available yet

use strict;

use lib '/home/biketempe/perl5/lib/perl5/';

use CGI;
use CGI::Carp 'fatalsToBrowser';
use Data::Dumper;
use IO::Handle;
use Text::CSV;
use List::MoreUtils 'zip';
use Cwd;
use JSON::PP;
use XXX;
use Carp;
use HTML::Scrubber;
use Email::Send::SMTP::Gmail;

use repop 'repop';
use csv;
use geo;
use geoip;

my %email_config = (
    -smtp=>   'smtp.gmail.com',
    -login=>  'bikecount@biketempe.org',
    -pass => `/home/biketempe/bin/bikecountgmail`,
    'port' =>  25,   # ports 485 (SMTP+SSL) and 587 (SMTP+TSL) are blocked, but 25 is apparently open
    -layer => 'ssl',
);

$SIG{USR1} = sub { Carp::confess $@; };
$SIG{ALRM} = sub { Carp::confess $@; };

alarm 60;

my $min_priority_to_include;
if( open my $fh, '<', 'min_priority.txt' ) {
    $min_priority_to_include = readline $fh;
    chomp $min_priority_to_include;
} else {
    $min_priority_to_include = 1000; # essentially everything
}

open my $log, '>>', 'signup.log' or die $!;
$log->autoflush(1);
$log->print('=' x 20, ' ', scalar localtime, ' ', '=' x 20, "\n");
$log->print("IP: $ENV{REMOTE_ADDR}\n");

close STDERR;
open STDERR, '>>', 'signup.log' or die $!;
STDERR->autoflush(1);


sub read_signupform {
    my $fn = shift;
    open my $fh, '<', $fn or die "$fn: $!";
    read $fh, my $signupform, -s $fh;
    close $fh;
    return $signupform;
}

# init volunteers

my $volunteers = csv->new('volunteers.csv', 0);

for my $column (qw/first_name last_name phone_number email_address training_session training_session_comment intersections comments/) {
    $volunteers->add_column($column) if ! grep $_ eq $column, @{ $volunteers->headers };
}

# init count sites

# don't flock
open my $count_sites_fh, '+<', 'count_sites.csv' or die $!;
seek $count_sites_fh, 0, 0;
my $count_sites = csv->new($count_sites_fh, 0);

geo::geocode( $count_sites );

#

sub get_pois {

    my $all_flag = shift;

    # get points of interest that haven't yet been (completely) allocated

    my $pending_sites;

    if( $all_flag ) {
        for my $site ( $count_sites->rows ) {
            $pending_sites->{ $site->location_id  } = $site;
        }
    } else {
        # normal case:  only show what's still available
        $pending_sites = get_pending_sites();
        my %only_do_each_location_once;
        for my $location_id_ampm ( keys %$pending_sites ) {
            delete $pending_sites->{ $location_id_ampm } if $only_do_each_location_once{ $pending_sites->{$location_id_ampm}->location_id }++;
        }
    }

    my @pois = sort { $a->{desc} cmp $b->{desc} } grep { $_->{lat} and $_->{lon} and $_->{desc} } map { 
        {
            lat  => $_->latitude,
            lon  => $_->longitude,
            desc => $_->location_id . ': ' . $_->location_N_S . ' and ' . $_->location_W_E,
            id   => $_->location_id,
        }
    } values %$pending_sites; # $count_sites->rows;
    # warn Data::Dumper::Dumper \@pois;

    return \@pois;
}


sub get_pending_sites {

    # returns a hash of 101A style codes to site records from $count_sites
    # takes an optional location_id argument to restrict results
    # enforced $min_priority_to_include

    my $loc_id = shift;

    my %sites;
    my %double_up;

    for my $site ( $count_sites->rows ) {
        next if $loc_id and $loc_id ne $site->location_id;
        next if ! $site->vols_needed;
        next if $site->priority < $min_priority_to_include;
        $double_up{ $site->location_id . 'A' } = $site->vols_needed;
        $double_up{ $site->location_id . 'P' } = $site->vols_needed;
# warn $site->location_id . ' gets ' . $site->vols_needed if $site->vols_needed > 1;
        $sites{ $site->location_id . 'A' } = $site;  # available until found otherwise
        $sites{ $site->location_id . 'P' } = $site;
    }

# warn "double_up: " . Dumper \%double_up;

    for my $volunteer ( $volunteers->rows ) {
        my $intersections = $volunteer->intersections or next;
        my @intersections = split m/,/, $intersections or next;
        for my $intersection ( @intersections ) {
            my( $location_id_ampm ) = $intersection =~ m/^(\d+[AP])/;  # ignore any trailing day of the week information
            next if $loc_id and $location_id_ampm !~ m/^$loc_id/;
            $double_up{ $location_id_ampm }--;
# warn "found an assignment to $location_id_ampm; $double_up{ $location_id_ampm } shifts remain";
            if( $double_up{ $location_id_ampm } >= 1 ) {
# warn "get pending sites hanging on to $location_id_ampm for now with this many slots left: $double_up{$location_id_ampm}";
             } else {
# warn "get pending sites just trashed $location_id_ampm";
                delete $sites{ $location_id_ampm };  # taken or no volunteers requested this year
            }
        }
    }

    # update unassigned_sites

    if( ! $loc_id ) {
        open my $fh, '>', 'unassigned_locations.txt' or warn $!;

        for my $id (sort { $a cmp $b } keys %sites) {
            my $site = $sites{$id};
            $fh and $fh->print($id, ': ', $site->location_N_S, ' and ', $site->location_W_E, "\n");
        }
    }

# warn "get pending sites: " . Dumper(\%sites);

    return \%sites;

}

sub get_compat_shifts {

    my $assignments = shift;
    my $pending_shifts = shift;

    # users existing assignments by shift

    my %assignment_by_date_shift;
    for my $assignment ( @$assignments ) {
        my( $location_id, $ampm, $day ) = $assignment =~ m/^(\d+)([AP])([A-Z][a-z]{2})$/ or die $assignment;
        $assignment_by_date_shift{ "$ampm$day" } = $location_id; # not checking here for double booked
# warn "already have an assignment for $ampm$day";
    }       

    # build a table of location_ids that are two person with one person on them already

    my %two_person_location;

    for my $site ( $count_sites->rows ) {
        next if $site->vols_needed < 2;
        $two_person_location{ $site->location_id . 'A' } = 1;
        $two_person_location{ $site->location_id . 'P' } = 1;
    }

    # figure out which two person locations already have one person on them so we can keep that same schedule
    # this builds a table of eg 101A => Tue

    my %already_started_two_person_shifts;

    for my $volunteer ( $volunteers->rows ) {
        my $intersections = $volunteer->intersections or next;
        my @intersections = split m/,/, $intersections or next;
        for my $intersection ( @intersections ) {
            my( $location_id_ampm, $day ) = $intersection =~ m/^(\d+[AP])([A-Z][a-z]{2})$/ or warn "bad intersection: $intersection";
            if( $two_person_location{ $location_id_ampm } ) {
                $already_started_two_person_shifts{ $location_id_ampm } = $day;
            }
        }
    }

    $log->print("already started two person shifts: " . Dumper(\%already_started_two_person_shifts) . "\n");

    # compatible shifts for a given location or set of locations

    my @okay_shifts;
    for my $shift (@$pending_shifts) {
# warn "pending shift $shift";
        my( $location_id, $ampm ) = $shift =~ m/^(\d+)([AP])/ or die $shift; # ignore any Tues Wed etc field
        if( exists $already_started_two_person_shifts{ "$location_id$ampm" } ) {
            # this is a multi-person intersection with one person already on it; if we have that day open, offer a shift on that same day
            my $day = $already_started_two_person_shifts{ "$location_id$ampm" };
# warn "$day is the day to double up $location_id$ampm";
            push @okay_shifts, "$location_id$ampm$day" if ! exists $assignment_by_date_shift{ "$ampm$day" };
        } else {
# warn "simple case for $location_id$ampm";
            # simple case of an unassigned one person intersection; offer all shifts that don't conflict
            for my $day ('Tue', 'Wed', 'Thu') {
                push @okay_shifts, "$location_id$ampm$day" if ! exists $assignment_by_date_shift{ "$ampm$day" };
            }
        }
    }

    return @okay_shifts;

}

sub get_assignments {
    my $email_address = shift or return;
    my $volunteer = $volunteers->find('email_address', $email_address, sub { lc $_[0] } ) or return;
    my $assignments = $volunteer->intersections or return;
    my @assignments = split m/,/, $assignments or return;
    return wantarray ? @assignments : \@assignments;
}

sub get_assignments_text {

    # returns a textual list of assignments for a given user

    my $email_address = lc(shift()) or return;
    my @assignments = get_assignments( $email_address );

    my $parsed_assignments = '';

    for my $intersection (@assignments) {
        my( $location_id, $ampm, $day ) = $intersection =~ m/(\d+)([AP])(.*)/;
        my $site = $count_sites->find('location_id', $location_id);
        $parsed_assignments .= "$day $ampm" .'M ' . $site->location_N_S . ' and ' . $site->location_W_E . " ($location_id)<br>\n";
    }

    return $parsed_assignments;

}

sub update_volunteer_data {

    # save user entered form data

    # XXX should subclass the volunteer records and add this logic there

    my $signup_data = shift;

    my $error;

    my $volunteer = $volunteers->find('email_address', $signup_data->{email_address}, sub { lc $_[0] } );

    if( ! $volunteer ) {
        $volunteer = $volunteers->add;
        $log->print("adding a new volunteer record\n");
        $volunteer->email_address = $signup_data->{email_address};
    }

    for my $key ( qw/first_name last_name phone_number training_session training_session_comment comments/ ) {
        if( $signup_data->{ $key } ) {
            $volunteer->{ $key } = $signup_data->{ $key };
            $log->print("setting $key = $signup_data->{$key} for user $signup_data->{email_address}\n");
        }
    }

    if( $signup_data->{location_id} ) {

        # record assignment

        my $assignment = $signup_data->{location_id};  # eg: 130: Country Club Wy and Alameda Dr
        $log->print("location_id = $assignment for user $signup_data->{email_address}\n");
        $assignment =~ s{:.*}{};  # comes in the form of eg "101: Hardy and Southern"
        $assignment .= $signup_data->{'shift'};  # eg: ATue
        $log->print("shift = $signup_data->{'shift'} for user $signup_data->{email_address}\n");
        $assignment =~ m/^\d{3}[AP][A-Z][a-z][a-z]$/ or do {
            $log->print("bad assignment: ``$assignment''\n");
            $log->print("ERROR --> bad assignement: ``$assignment''\n");
            return "<br><br>Error:  Pick a location and a shift";
        };
        $log->print("new assignment: $assignment\n");

        if( ! grep $_ eq "$assignment", get_compat_shifts( [ split(',', $volunteer->intersections) ], [ $assignment ])) {
         
            $error = '<br><br>Count shift would conflict.  Not added!';

        } else {

            my $intersections = $volunteer->intersections;
            $intersections .= ',' if $intersections;
            $intersections .= $assignment;
            $volunteer->intersections = $intersections;
            $error = '<br><br>Count shift recorded -- thanks!';

            eval {
                # send an email to ourselves
                my $name = "$signup_data->{first_name} $signup_data->{last_name} $signup_data->{email_address}";
                my $body = <<EOF;
Hi there,

$name just signed up for shift $assignment.
EOF
                $body .= "Phone number: $signup_data->{phone_number}\n" if $signup_data->{phone_number};
                $body .= "Comments: $signup_data->{comments}\n" if $signup_data->{comments};
                $body .= "Training session other field: $signup_data->{training_session_comment}\n" if $signup_data->{training_session_comment};
                my $mail = Email::Send::SMTP::Gmail->new( %email_config ) or die;
                $mail->send(
                    -to => $email_config{'-login'},  # to ourselves
                    -subject => "$name signed up for shift $assignment" . ( $signup_data->{comments} ? ' [with comments]' : ''),
                    -body => $body,
                ); # or die; # always dies
            };

            eval {
                # send an email to them
                my $name = "$signup_data->{first_name} $signup_data->{last_name} ($signup_data->{email_address})";

                my $body = <<EOF;
Hi @{[ $signup_data->{first_name} || $name ]},

Thank you for being a bike count volunteer!  It's people like you that make stuff happen.

Please take a moment to double check your information.  Let bikecount\@biketempe.org know
if any corrections are needed.

Assignment:  $assignment
Phone number: $signup_data->{phone_number}
Comments: $signup_data->{comments}
Training session:  $signup_data->{training_session}
Training session other field: $signup_data->{training_session_comment}

Thank you,
The Bike Count Team

EOF
                my $mail = Email::Send::SMTP::Gmail->new( %email_config ) or die;
                $mail->send(
                    -to =>  $signup_data->{email_address},
                    -subject => "Thank you for signing up for the $assignment bike count shift!",
                    -body => $body,
                );
            };

        }

    }

    $volunteers->write;
    chmod 0640, "volunteers.csv";

    return $error;

}

my $scrubber = HTML::Scrubber->new;

my $req = CGI->new;

# request handler

do {

    my $action = $req->param('action') || 'default';
    my $email = $req->param('email');
    $log->print("action = $action\n");
    $log->print("email = $email\n");
    $log->print("user agent = $ENV{HTTP_USER_AGENT}\n"); # who is sending email = [object HTMLInputElement]?

    my $mapnote = '';
    $mapnote = '<br><font color="red">iOS Users:  Use the select box below the map.</font>' if $ENV{HTTP_USER_AGENT} =~ m{iPhone} or $ENV{HTTP_USER_AGENT} =~ m{iPad};

    my $signup_data = { };
    my $error;

    $req->print("Content-type: text/html\r\n\r\n");

    if( $action eq 'register' ) {

        # my %new_params = $req->param;
        my %new_params = map { $_ => scalar $req->param($_) } $req->param; # CGI's param() returns a list of names
        warn "new params: " . Data::Dumper::Dumper \%new_params;

        for my $new_param (keys %new_params) {
            next if $new_param eq 'action';
            next unless $new_params{ $new_param };
            $signup_data->{ $new_param } = $scrubber->scrub( $new_params{ $new_param } );
        }
        $log->print("signup_data: " . Data::Dumper::Dumper $signup_data );

        if(geoip::geoip($ENV{REMOTE_ADDR}) ne 'United States') {
            # don't record anything; dealing with spam
            $error = '<br><br>Count shift recorded -- thanks!';
        } else {
            # United States; record stuff
            $error = update_volunteer_data( $signup_data ) || '' if $signup_data->{email_address};
        }

        # fall through to showing the signup form again, repopulated with $signup_data

    } elsif( $action eq 'get_times_for_intersection' ) {
        
        my $location_id = $scrubber->scrub( $req->param('location_id') );
        $location_id =~ s{:.*}{};  # comes in the form of eg "101: Hardy and Southern"

        my $sites = get_pending_sites( $location_id );
        $sites = [ sort { $a cmp $b } keys %$sites ];
        # warn "pending sites = @$sites";
        my @open_shifts = get_compat_shifts( scalar(get_assignments( $email )), $sites );
        # warn "open_shifts = @open_shifts";

        for my $shift ( @open_shifts ) {
            my( $location_id, $ampm, $day ) = $shift =~ m/^(\d{3})([AP])([A-Z][a-z][a-z])$/;
            # warn "shift = $shift day = $day ampm = $ampm";
            my $nice_day = { Tue => 'Tuesday', Wed => 'Wednesday', Thu => 'Thursday', }->{$day};
            my $nice_ampm = { P => 'PM', A => 'AM', }->{$ampm};
            $req->print(qq{    <li class="ss-choice-item"><label class="ss-choice-label"><input name="shift" class="ss-q-radio" type="radio" value="$ampm$day"/>$nice_day $nice_ampm</label></li>\n});
        }

        if( ! @open_shifts ) {
            $req->print(qq{Either your AM or PM is full and your schedule cannot accommodate these shifts: @$sites.<br>\n});
        }
        exit;

    } elsif( $action eq 'get_assignments' ) {

        my $assignments = get_assignments_text( $email );
        $req->print( $assignments || 'No current assignments for that email address' );
        $log->print("current assignments for that user are:\n$assignments\n");
        exit;

    }

    # default:  show the signup form

    my $all = $req->param('all');  # show all intersections, even those that are full?

    my $signupform = read_signupform('signup2.html');

    my $html = repop( $signupform, $signup_data );

    my $pois = get_pois( $all );
    my $json_pois = encode_json $pois;
    $html =~ s/POIS/$json_pois/;

    my $available_intersections = '';
    for my $poi (@$pois) {
        $available_intersections .= qq{
            <option value="@{[ $poi->{desc} ]}">@{[ $poi->{desc} ]}</option>
        };
    }
    $html =~ s/AVAILABLEINTERSECTIONS/$available_intersections/;

    my $assignments = get_assignments_text( $email );
    $html =~ s/CURRENT_ASSIGNMENTS/$assignments/;

    my $comments = $signup_data->{comments} || '';
    $html =~ s/COMMENTS/$comments/;

    $html =~ s/ERROR/$error/;

    $html =~ s/MAPNOTE/$mapnote/;

    $req->print( $html );

    alarm 0;
   
}



